"""Turbo PATCH permission boundary (edit level), toggle log line and session.turbo SSE event."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.routes._sessions.common import _SERVER_STREAM_EVENT_ADAPTER
from tests.server.helpers import create_test_agent, start_session_stream_collector
from tests.server.integration.test_sessions_permissions import (  # noqa: F401
    _create_session_as,
    _grant_permission,
    auth_app,
    auth_client,
)

pytestmark = pytest.mark.asyncio


def _h(u: str) -> dict[str, str]:
    return {"X-Forwarded-Email": u}


async def test_turbo_patch_permission_boundary(
    auth_client: httpx.AsyncClient,  # noqa: F811
) -> None:
    agent = await create_test_agent(auth_client, user="owner@x.io")
    sid = (await _create_session_as(auth_client, agent["id"], "owner@x.io"))["id"]
    assert (await auth_client.get(f"/v1/sessions/{sid}", headers=_h("owner@x.io"))).json()[
        "turbo"
    ] is False
    for user, level in (("reader@x.io", LEVEL_READ), ("editor@x.io", LEVEL_EDIT)):
        r = await _grant_permission(
            auth_client, sid, granter="owner@x.io", target_user=user, level=level
        )
        assert r.status_code == 200, r.text
    r = await auth_client.patch(
        f"/v1/sessions/{sid}", json={"turbo": True}, headers=_h("reader@x.io")
    )
    assert r.status_code == 403, (r.status_code, r.text)
    r = await auth_client.patch(
        f"/v1/sessions/{sid}", json={"turbo": True}, headers=_h("stranger@x.io")
    )
    assert r.status_code in (403, 404), (r.status_code, r.text)
    # Comparator: the same read-level user is likewise blocked from model_override.
    r = await auth_client.patch(
        f"/v1/sessions/{sid}", json={"model_override": "gpt-5"}, headers=_h("reader@x.io")
    )
    assert r.status_code == 403, (r.status_code, r.text)
    assert (await auth_client.get(f"/v1/sessions/{sid}", headers=_h("owner@x.io"))).json()[
        "turbo"
    ] is False
    r = await auth_client.patch(
        f"/v1/sessions/{sid}", json={"turbo": True}, headers=_h("editor@x.io")
    )
    assert r.status_code == 200, r.text
    assert r.json()["turbo"] is True
    # Read-level user sees it on the snapshot.
    assert (await auth_client.get(f"/v1/sessions/{sid}", headers=_h("reader@x.io"))).json()[
        "turbo"
    ] is True


async def _next_turbo(collector, timeout: float = 2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            evt = await collector.next_event(timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if evt.get("type") == "session.turbo":
            return evt


async def test_turbo_log_and_sse(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    agent = await create_test_agent(client)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"], "initial_items": []})
    sid = resp.json()["id"]
    collector = await start_session_stream_collector(sid)
    try:
        with caplog.at_level(logging.INFO, logger="omnigent.server.turbo"):
            r = await client.patch(f"/v1/sessions/{sid}", json={"turbo": True})
            assert r.status_code == 200
            evt = await _next_turbo(collector)
            assert evt is not None and evt["conversation_id"] == sid and evt["turbo"] is True, evt
            validated = _SERVER_STREAM_EVENT_ADAPTER.validate_python(evt)
            assert type(validated).__name__ == "SessionTurboEvent"
            msgs = [
                rec.getMessage() for rec in caplog.records if rec.name == "omnigent.server.turbo"
            ]
            assert (
                len(msgs) == 1
                and msgs[0].startswith(f"turbo session={sid} user=")
                and msgs[0].endswith(" off->on")
            ), msgs
            caplog.clear()
            # No-op PATCH (already on): no log line, no event.
            r = await client.patch(f"/v1/sessions/{sid}", json={"turbo": True})
            assert r.status_code == 200
            assert await _next_turbo(collector, 0.4) is None
            assert not [rec for rec in caplog.records if rec.name == "omnigent.server.turbo"]
            # Off again: on->off logged + event.
            r = await client.patch(f"/v1/sessions/{sid}", json={"turbo": False})
            evt = await _next_turbo(collector)
            assert evt is not None and evt["turbo"] is False
            msgs = [
                rec.getMessage() for rec in caplog.records if rec.name == "omnigent.server.turbo"
            ]
            assert len(msgs) == 1 and msgs[0].endswith(" on->off"), msgs
    finally:
        await collector.stop()
