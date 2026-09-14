"""Explicit agent MCP registration reaches the Antigravity native launch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.runner.native import orchestration


@pytest.mark.asyncio
async def test_antigravity_adapter_forwards_bound_agent_spec(monkeypatch):
    spec = object()
    launch = AsyncMock(return_value="terminal")
    monkeypatch.setattr(orchestration, "_auto_create_antigravity_terminal", launch)
    ctx = SimpleNamespace(
        session_id="fixture-session",
        resource_registry=object(),
        publish_event=object(),
        server_client=object(),
        ensure_comment_relay=None,
        agent_spec=spec,
    )
    assert await orchestration._launch_antigravity(ctx) == "terminal"
    assert launch.await_args.kwargs["agent_spec"] is spec


@pytest.mark.asyncio
async def test_antigravity_builder_renders_bound_agent_servers(tmp_path, monkeypatch):
    from omnigent import antigravity_native_bridge, antigravity_native_launch

    declared = SimpleNamespace(
        name="personal-cloud", transport="stdio", command="/fixture/bridge", args=[], env={}
    )
    spec = SimpleNamespace(mcp_servers=[declared])
    monkeypatch.setattr(
        orchestration,
        "_session_payload_for_host_spawn_check",
        AsyncMock(return_value={"workspace": str(tmp_path)}),
    )
    monkeypatch.setattr(orchestration, "_cancel_auto_forwarder_task", AsyncMock())
    monkeypatch.setattr(antigravity_native_bridge, "prepare_bridge_dir", lambda _: tmp_path)
    monkeypatch.setattr(antigravity_native_bridge, "clear_bridge_state", lambda _: None)
    monkeypatch.setattr(antigravity_native_launch, "build_agy_launch", lambda **_: (["agy"], {}))

    class RenderReached(Exception):
        pass

    def capture_render(bridge_dir, *, servers):
        assert bridge_dir == tmp_path
        assert servers == [declared]
        raise RenderReached

    monkeypatch.setattr(antigravity_native_bridge, "write_mcp_config", capture_render)
    with pytest.raises(RenderReached):
        await orchestration._auto_create_antigravity_terminal(
            "fixture-session",
            object(),
            lambda *_: None,
            agent_spec=spec,
            server_client=object(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["create", "ensure"])
async def test_antigravity_routes_resolve_bound_spec(tmp_path, monkeypatch, route):
    from starlette.responses import JSONResponse

    from omnigent.runner import create_runner_app
    from omnigent.spec.types import AgentSpec, ExecutorSpec
    from omnigent.terminals import TerminalRegistry
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
    from tests.runner.test_app_sessions_native_terminals_autocreate import (
        _AntigravitySnapshotServerClient,
    )

    monkeypatch.setattr("omnigent.antigravity_native_bridge._BRIDGE_ROOT", tmp_path)
    native_spec = AgentSpec(
        spec_version=1,
        name="fixture-personal",
        executor=ExecutorSpec(type="omnigent", config={"harness": "antigravity-native"}),
    )

    async def resolve(*_args, **_kwargs):
        return native_spec

    captured = []

    async def capture(*_args, agent_spec=None, **_kwargs):
        captured.append(agent_spec)
        return

    monkeypatch.setattr(orchestration, "_auto_create_antigravity_terminal", capture)
    monkeypatch.setattr(
        orchestration,
        "_ensure_native_terminal_default_response",
        lambda _: JSONResponse({"fixture": True}),
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        spec_resolver=resolve,
        server_client=_AntigravitySnapshotServerClient("fixture-personal"),
        terminal_registry=TerminalRegistry(),
    )
    session_id = "2d1b1a96e3e08f2cd43c0cc4b695ac5d"
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
        assert response.status_code == 201, response.text
        if route == "ensure":
            captured.clear()
            response = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={
                    "terminal": "antigravity",
                    "session_key": "main",
                    "ensure_native_terminal": True,
                },
            )
            assert response.status_code == 200, response.text
    assert captured == [native_spec]
