"""Runner authentication survives temporary loss of its token endpoint."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
import pytest

from omnigent.runner import _entry as entry


def test_initial_factory_retries_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed discovery must not permanently disable a running session."""
    recovered = False

    def resolve(*args: object, **kwargs: object) -> Callable[[], str] | None:
        return (lambda: "recovered-token") if recovered else None

    monkeypatch.setattr(entry, "_make_auth_token_factory", resolve)
    factory = entry._InitialAuthTokenFactory("bootstrap", "https://server.example")
    factory.invalidate()
    assert factory() is None
    recovered = True
    assert factory() == "recovered-token"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_mint_retries_temporary_http_failure(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """A short server outage is absorbed before a callback is denied."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status)
        return httpx.Response(200, json={"token": "fresh", "expires_at": time.time() + 1800})

    real_client = httpx.Client
    monkeypatch.setattr(
        entry.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(entry.time, "sleep", lambda _: None)
    token, _ = entry._mint_managed_owner_token(
        "https://server.example/v1/runners/r/token", "https://server.example", "binding"
    )
    assert token == "fresh"
    assert len(calls) == 2


def test_mint_failure_reports_server_status_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report the actual failure without secrets, then recover in the same factory."""
    recovered = False

    def mint(*args: object, **kwargs: object) -> tuple[str, float]:
        if recovered:
            return "fresh", time.time() + 1800
        request = httpx.Request("POST", "https://server.example/v1/runners/r/token")
        raise httpx.HTTPStatusError(
            "private upstream response",
            request=request,
            response=httpx.Response(502, request=request),
        )

    monkeypatch.setattr(entry, "_mint_managed_owner_token", mint)
    factory = entry._ManagedMintTokenFactory(
        "https://server.example/v1/runners/r/token", "https://server.example", "secret-binding"
    )
    recovered = True
    assert factory() == "fresh"
    recovered = False
    factory._cached_expires_at = time.time() - 1
    auth = entry._RunnerDatabricksAuth(factory)
    with pytest.raises(httpx.RequestError, match=r"Omnigent.*HTTP 502") as exc:
        next(auth.auth_flow(httpx.Request("GET", "https://server.example/api/version")))
    assert "private upstream response" not in str(exc.value)
    assert "secret-binding" not in str(exc.value)
    assert "Databricks" not in str(exc.value)
    recovered = True
    sent = next(auth.auth_flow(httpx.Request("GET", "https://server.example/api/version")))
    assert sent.headers["Authorization"] == "Bearer fresh"
    assert factory.failure_reason is None


def test_mint_does_not_retry_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential rejection must remain a rejection."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401)

    real_client = httpx.Client
    monkeypatch.setattr(
        entry.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(httpx.HTTPStatusError):
        entry._mint_managed_owner_token(
            "https://server.example/v1/runners/r/token", "https://server.example", "binding"
        )
    assert len(calls) == 1


def test_mint_retries_transport_error_and_bounds_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Persistent network failures stop after three safe mint attempts."""
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectTimeout("private transport diagnostic")

    real_client = httpx.Client
    monkeypatch.setattr(
        entry.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(entry.time, "sleep", lambda _: None)
    with pytest.raises(httpx.ConnectTimeout):
        entry._mint_managed_owner_token(
            "https://server.example/v1/runners/r/token", "https://server.example", "binding"
        )
    assert len(calls) == 3
