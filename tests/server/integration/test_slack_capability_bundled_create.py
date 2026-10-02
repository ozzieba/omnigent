"""
The Slack orchestrator capability cannot reach the multipart bundled create.

``POST /v1/sessions`` is one of the four operation shapes the auth layer
admits for a ``slack-orchestrator`` token, and the JSON branch of
``create_session`` holds such a token to a reviewed template. The
multipart branch is dispatched first, on Content-Type alone, and stores an
arbitrary caller-supplied agent bundle. Before the fix it never consulted
the capability, so a restricted token could create (and own) a session
running any agent, and label it with its own grant id so the next
capability list registered it and let the token message it.

These tests drive the real route. The ASGI shim stands in for the auth
layer, which records ``oauth_scope`` on the request state for a validated
capability token (see ``UnifiedAuthProvider``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.slack_orchestrator_capability import GRANT_LABEL, SCOPE
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio


class _AsCapabilityToken:
    """Mark every HTTP request as carrying a Slack orchestrator capability."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {})["oauth_scope"] = SCOPE
        await self._app(scope, receive, send)


async def _bundled_create(client: httpx.AsyncClient, metadata: dict[str, Any]) -> httpx.Response:
    bundle = build_agent_bundle(name="slack-capability-bundle")
    return await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )


async def test_capability_token_cannot_use_multipart_bundled_create(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """A capability token is refused before any bundle is stored."""
    transport = httpx.ASGITransport(app=_AsCapabilityToken(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as capability:
        resp = await _bundled_create(capability, {})
    assert resp.status_code == 403, resp.text

    listed = await client.get("/v1/sessions")
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"] == [], "the refused bundled create still stored a session"


async def test_multipart_bundled_create_rejects_the_grant_label(
    client: httpx.AsyncClient,
) -> None:
    """No caller may seed the server-owned grant label through a bundle upload.

    The capability list registers any session carrying its grant id, so a
    forged label would hand that session to the capability.
    """
    resp = await _bundled_create(client, {"labels": {GRANT_LABEL: "grant-x"}})
    assert resp.status_code == 403, resp.text


async def test_multipart_bundled_create_still_works_without_a_capability(
    client: httpx.AsyncClient,
) -> None:
    """The ordinary bundled create is unchanged."""
    resp = await _bundled_create(client, {"labels": {"team": "ml"}})
    assert resp.status_code == 201, resp.text
    assert "session_id" in resp.json()
