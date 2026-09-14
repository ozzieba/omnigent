"""Runner-owned OpenCode servers retain their child identity at launch."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent import opencode_native_app_server, opencode_native_bridge
from omnigent import opencode_native_provider as provider
from omnigent.runner.native import orchestration


async def test_auto_create_opencode_passes_child_identity_without_policy_relay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child = "22222222222222222222222222222222"
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "1" * 32)
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)
    monkeypatch.setattr(opencode_native_bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    monkeypatch.setattr(opencode_native_bridge, "write_relay_bridge_config", lambda _p: None)
    monkeypatch.setattr(opencode_native_bridge, "seed_opencode_auth", lambda _p: None)
    monkeypatch.setattr(provider, "resolve_databricks_gateway", lambda *_a, **_k: None)
    monkeypatch.setattr(provider, "maybe_merge_user_provider_config", lambda config: config)
    monkeypatch.setattr(orchestration, "_cancel_auto_forwarder_task", AsyncMock())
    monkeypatch.setattr(
        orchestration,
        "_opencode_native_launch_config",
        AsyncMock(
            return_value=orchestration._OpenCodeNativeLaunchConfig(
                workspace=tmp_path,
                policy_server_url="http://127.0.0.1:1",
                terminal_launch_args=None,
                model_override=None,
                external_session_id=None,
            )
        ),
    )

    class LaunchCaptured(Exception):
        pass

    def capture_server(**kwargs: object) -> None:
        assert kwargs.get("session_id") == child
        raise LaunchCaptured

    monkeypatch.setattr(opencode_native_app_server, "OpenCodeNativeServer", capture_server)
    with pytest.raises(LaunchCaptured):
        await orchestration._auto_create_opencode_terminal(
            child,
            None,  # type: ignore[arg-type]  # Constructor capture precedes registry access.
            lambda *_args: None,
        )
