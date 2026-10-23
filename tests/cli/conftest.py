"""Shared fixtures for CLI unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the machine's own host identity and runner slice-key env var.

    ``databricks_request_headers`` (reached via ``_remote_headers``,
    ``open_daemon_client``, and the auth flow) resolves the slice key from,
    in order: an explicit ``host_id``, the ``OMNIGENT_RUNNER_SLICE_KEY`` env
    var (set inside a runner process), then the CLI's own host identity. On a
    developer machine that IS a host — a persisted ``~/.omnigent/config.yaml``
    ``host:`` section — that last fallback would leak the machine's host_id
    into requests, so any "no slice key on this call" assertion would flake
    depending on where the suite runs. Force the read-only lookup to ``None``
    and clear the runner env so CLI tests exercise only the host they name.

    ``autouse=True`` keeps the isolation on by default; tests that need a real
    host identity re-patch ``load_host_identity_if_present`` themselves.
    """
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda *a, **k: None,
    )
    monkeypatch.delenv("OMNIGENT_RUNNER_SLICE_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_real_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a CLI unit test spawn a real host daemon or local server.

    ``omnigent claude`` / ``codex`` / ... call ``cli._ensure_backend`` before
    handing off to the (stubbed) native wrapper. Un-stubbed, that starts a real
    host daemon plus a local Omnigent server (or a remote tunnel for
    ``--server https://...``) from inside a unit test, which takes 10-30 s on a
    loaded runner and fails with "host daemon for local did not claim its
    registry record within 10s". Resolve the backend to the requested server
    (or a dead loopback URL in local mode) instead.

    ``autouse=True`` keeps this on by default. Tests that assert on
    ``_ensure_backend`` itself import it directly (``from omnigent.cli import
    _ensure_backend``) and are unaffected; tests that need a specific return
    value re-patch ``omnigent.cli._ensure_backend`` themselves.
    """
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend",
        lambda server=None, *_a, **_k: server or "http://localhost:0",
    )
