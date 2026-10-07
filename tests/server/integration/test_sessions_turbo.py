"""Integration tests for the per-session Turbo switch.

oz, 2026-10-06: "gate via llmq, but with a per session 'turbo' button on omnigent
right next to the model selector"; 2026-10-07: "this is not a security boundary".
Turbo is a plain boolean stored with the other session overrides: off by default,
settable at create, toggled by ``PATCH /v1/sessions/{id}`` at edit level, served on
the snapshot, carried in the runner's session-init envelope, and announced as a
``session.turbo`` SSE event so every open surface re-renders its chip.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runner.session_init_protocol import (
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(
    client: httpx.AsyncClient, agent_id: str, **extra: Any
) -> dict[str, Any]:
    resp = await client.post(
        "/v1/sessions", json={"agent_id": agent_id, "initial_items": [], **extra}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_turbo_is_off_by_default_and_patch_round_trips(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    assert session["turbo"] is False

    resp = await client.patch(f"/v1/sessions/{session['id']}", json={"turbo": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["turbo"] is True
    snapshot = await client.get(f"/v1/sessions/{session['id']}")
    assert snapshot.json()["turbo"] is True

    # An omitted field leaves it; an explicit false clears it.
    resp = await client.patch(f"/v1/sessions/{session['id']}", json={"title": "kept"})
    assert resp.json()["turbo"] is True
    resp = await client.patch(f"/v1/sessions/{session['id']}", json={"turbo": False})
    assert resp.json()["turbo"] is False
    assert (await client.get(f"/v1/sessions/{session['id']}")).json()["turbo"] is False


async def test_turbo_can_be_set_at_create(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"], turbo=True)
    assert session["turbo"] is True
    assert (await client.get(f"/v1/sessions/{session['id']}")).json()["turbo"] is True
    plain = await _create_session(client, agent["id"], turbo=False)
    assert plain["turbo"] is False


async def test_turbo_survives_other_override_writes(client: httpx.AsyncClient) -> None:
    """Turbo shares the packed overrides blob with the model switches."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"], turbo=True)
    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"reasoning_effort": "high", "cost_control_mode_override": "off"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["turbo"] is True
    assert body["cost_control_mode_override"] == "off"


async def test_turbo_rides_in_the_runner_session_init_envelope(client: httpx.AsyncClient) -> None:
    from omnigent.entities.conversation import Conversation

    conversation = Conversation(
        id="conv_turbo",
        created_at=10,
        updated_at=11,
        root_conversation_id="conv_turbo",
        agent_id="agent_init",
        runner_id="runner_init",
        turbo=True,
    )
    envelope = parse_runner_session_init_envelope(
        build_runner_session_init_payload(conversation, server_version="0.6.0.dev0")
    )
    assert envelope is not None
    assert envelope.snapshot.turbo is True
    plain = parse_runner_session_init_envelope(
        build_runner_session_init_payload(
            Conversation(
                id="conv_plain",
                created_at=10,
                updated_at=11,
                root_conversation_id="conv_plain",
                agent_id="agent_init",
                runner_id="runner_init",
            ),
            server_version="0.6.0.dev0",
        )
    )
    assert plain is not None and plain.snapshot.turbo is False
