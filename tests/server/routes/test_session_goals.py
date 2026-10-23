"""HTTP readback and native message-boundary delivery."""

from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities.session_goal import SetSessionGoalRequest
from omnigent.inner.opencode_native_executor import _content_to_native_prompt
from omnigent.opencode_http_transport import build_prompt_payload
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def native_session(store, wrapper):
    conv = store.create_conversation()
    store.set_labels(conv.id, {"omnigent.wrapper": wrapper})
    return store.get_conversation(conv.id)


@pytest.mark.parametrize("wrapper", ["claude-code-native-ui", "opencode-native-ui"])
async def test_offline_write_readback(client, db_uri, wrapper):
    s = SqlAlchemyConversationStore(db_uri)
    sid = native_session(s, wrapper).id
    url = f"/v1/sessions/{sid}/goals"
    assert (await client.get(url)).json() == {"goal": None}
    r = await client.put(url, json={"objective": "Restore useful data", "expected_revision": 0})
    assert r.status_code == 200
    assert r.json()["goal"]["status"] == "paused"
    assert (await client.get(url)).json() == r.json()
    assert s.get_conversation(sid).runner_id is None
    assert s.list_items(sid).data == []
    assert (
        await client.put(url, json={"objective": "Stale", "expected_revision": 0})
    ).status_code == 409
    assert (await client.get(url)).json() == r.json()


@pytest.mark.parametrize(
    "body",
    [
        {"objective": "  ", "expected_revision": 0},
        {"objective": "Good", "expected_revision": True},
        {"objective": "Good", "expected_revision": 0, "token_budget": 20},
        {"objective": "Good", "expected_revision": 0, "status": "completed"},
    ],
)
async def test_invalid_request(client, db_uri, body):
    s = SqlAlchemyConversationStore(db_uri)
    sid = native_session(s, "opencode-native-ui").id
    assert (await client.put(f"/v1/sessions/{sid}/goals", json=body)).status_code == 422
    assert s.get_session_goal(sid) is None


async def test_codex_separate(client, db_uri):
    s = SqlAlchemyConversationStore(db_uri)
    sid = native_session(s, "codex-native-ui").id
    assert (
        await client.put(
            f"/v1/sessions/{sid}/goals",
            json={
                "objective": "Wrong backend",
                "expected_revision": 0,
            },
        )
    ).status_code == 400
    assert s.get_session_goal(sid) is None


@pytest.mark.parametrize("wrapper", ["claude-code-native-ui", "opencode-native-ui"])
@pytest.mark.parametrize("state", ["active", "paused", "blocked"])
async def test_delivery(db_uri, wrapper, state, monkeypatch):
    s = SqlAlchemyConversationStore(db_uri)
    c = native_session(s, wrapper)
    g = s.set_session_goal(
        c.id,
        SetSessionGoalRequest(
            objective="Verify independent backup hashes",
            expected_revision=0,
            status=state,
        ),
        "owner",
    )
    forward = AsyncMock()
    monkeypatch.setattr(orchestration, "_forward_native_terminal_message", forward)
    original = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "Read the receipt"}],
            "goal": {"objective": "Caller spoof", "status": "active"},
        },
    )
    async with httpx.AsyncClient() as transport:
        result = await orchestration._dispatch_session_event_to_runner_impl(
            c.id,
            c,
            original,
            s,
            transport,
            agent_name=None,
            file_store=None,
            artifact_store=None,
            native_terminal_ready=True,
        )
    b = forward.call_args.args[3]
    event = orchestration._build_native_terminal_message_event(c, b)
    content = event["content"]
    assert content[0]["text"] == "Read the receipt"
    assert "Verify independent backup hashes" in content[-1]["text"]
    assert f'"status": "{state}"' in content[-1]["text"]
    assert "Caller spoof" not in content[-1]["text"]
    assert ("Do not autonomously resume" in content[-1]["text"]) == (state != "active")
    assert original.data["content"] == [{"type": "input_text", "text": "Read the receipt"}]
    assert s.get_session_goal(c.id) == g
    assert "model_override" not in event
    if wrapper == "opencode-native-ui":
        p = build_prompt_payload(_content_to_native_prompt(content))
        assert "Verify independent backup hashes" in p["parts"][0]["text"]
    pending_inputs.resolve(c.id, result.pending_id)


async def test_permission_levels_hide_other_owners(db_uri, runtime_init, tmp_path):
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    s = SqlAlchemyConversationStore(db_uri)
    p = SqlAlchemyPermissionStore(db_uri)
    sid = native_session(s, "claude-code-native-ui").id
    p.grant("owner", sid, 4)
    p.grant("reader", sid, 1)
    a = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=s,
        artifact_store=a,
        agent_cache=AgentCache(artifact_store=a, cache_dir=tmp_path / "cache"),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
        permission_store=p,
    )
    url = f"/v1/sessions/{sid}/goals"
    data = {"objective": "Owner objective", "expected_revision": 0}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        assert (
            await c.put(url, json=data, headers={"X-Forwarded-Email": "owner"})
        ).status_code == 200
        assert (await c.get(url, headers={"X-Forwarded-Email": "reader"})).status_code == 200
        assert (
            await c.put(url, json=data, headers={"X-Forwarded-Email": "reader"})
        ).status_code == 403
        assert (await c.get(url, headers={"X-Forwarded-Email": "stranger"})).status_code == 404
        assert (
            await c.put(url, json=data, headers={"X-Forwarded-Email": "stranger"})
        ).status_code == 404
        assert (await c.get(url)).status_code == 401
    assert s.get_session_goal(sid).revision == 1


async def test_claude_executor_delivers_goal_context(tmp_path, monkeypatch):
    from omnigent.entities.session_goal import SessionGoal
    from omnigent.inner import claude_native_executor
    from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
    from omnigent.inner.executor import TurnComplete
    from omnigent.runtime.prompt import append_session_goal_context

    goal = SessionGoal(
        session_id="0" * 32,
        objective="Verify backup hashes",
        status="paused",
        revision=1,
        created_at=1,
        updated_at=1,
    )
    content = append_session_goal_context([{"type": "input_text", "text": "Read receipt"}], goal)
    captured = []

    def inject(bridge_dir, *, content, timeout_s=30):
        captured.append(content)

    monkeypatch.setattr(claude_native_executor, "inject_user_message", inject)
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    events = [
        e
        async for e in executor.run_turn(
            messages=[{"role": "user", "content": content}], tools=[], system_prompt="original"
        )
    ]
    assert events == [TurnComplete(response=None)]
    assert len(captured) == 1
    assert captured[0].startswith("Read receipt")
    assert "Verify backup hashes" in captured[0]
    assert "Do not autonomously resume" in captured[0]
