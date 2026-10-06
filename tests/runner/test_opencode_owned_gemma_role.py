"""Owned Gemma role config and pre-launch cache protection."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.opencode_native_provider import (
    OPENCODE_OWNED_GEMMA_MODEL,
    OPENCODE_OWNED_GEMMA_ROLE,
    project_owned_gemma_tool_config,
    write_opencode_provider_config,
)
from omnigent.runner.native import orchestration as native


def _spec(role: object = OPENCODE_OWNED_GEMMA_ROLE) -> SimpleNamespace:
    return SimpleNamespace(
        executor=SimpleNamespace(config={"opencode_tool_role": role}), mcp_servers=[]
    )


def _launch(tmp_path: Path, **changes: object) -> native._OpenCodeNativeLaunchConfig:
    launch = native._OpenCodeNativeLaunchConfig(
        workspace=tmp_path,
        policy_server_url="http://central.test",
        terminal_launch_args=None,
        model_override=OPENCODE_OWNED_GEMMA_MODEL,
        external_session_id=None,
    )
    return dataclasses.replace(launch, **changes)


def test_generated_config_keeps_provider_and_native_ask_without_env_secrets(
    tmp_path: Path,
) -> None:
    config = {
        "model": OPENCODE_OWNED_GEMMA_MODEL,
        "provider": {"p16v": {"options": {"apiKey": "fixture-secret"}}},
        "mcp": {"omnigent": {"enabled": True}},
        "permission": "ask",
        "plugin": ["policy.js"],
    }
    projected, env = project_owned_gemma_tool_config(
        config, bridge_dir=tmp_path, policy_plugin="policy.js"
    )
    written = json.loads(write_opencode_provider_config(tmp_path, projected).read_text())
    assert written["provider"] == config["provider"]
    assert written["mcp"] == config["mcp"]
    assert written["plugin"] == config["plugin"]
    assert written["permission"] == {
        "*": "deny",
        "read": "ask",
        "edit": "ask",
        "bash": "ask",
        "glob": "ask",
        "grep": "ask",
    }
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    overlay = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert overlay == {"model": written["model"], "permission": written["permission"]}
    assert "fixture-secret" not in str(env)
    assert config["permission"] == "ask"


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "p16v/other"),
        ("mcp", {"other": {}}),
        ("plugin", ["custom.js"]),
        ("permission", {"bash": "allow"}),
    ],
)
def test_unknown_config_producers_fail_closed(tmp_path: Path, field: str, value: object) -> None:
    config = {"model": OPENCODE_OWNED_GEMMA_MODEL, "permission": "ask", field: value}
    with pytest.raises(ValueError):
        project_owned_gemma_tool_config(config, bridge_dir=tmp_path)


@pytest.mark.parametrize(
    "changes",
    [
        {"external_session_id": "existing"},
        {"fork_carry_history": True},
        {"terminal_launch_args": ["--model", "other/model"]},
        {"model_override": "other/model"},
    ],
)
async def test_invalid_role_launch_preserves_cached_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object]
) -> None:
    cached = SimpleNamespace(close=AsyncMock())
    monkeypatch.setitem(native._AUTO_OPENCODE_SERVERS, "role-test", cached)
    monkeypatch.setattr(
        native,
        "_opencode_native_launch_config",
        AsyncMock(return_value=_launch(tmp_path, **changes)),
    )
    monkeypatch.setattr(
        native, "_opencode_native_model_from_spec", lambda _: OPENCODE_OWNED_GEMMA_MODEL
    )
    cancel = AsyncMock()
    monkeypatch.setattr(native, "_cancel_auto_forwarder_task", cancel)
    with pytest.raises(ValueError, match="Owned Gemma role"):
        await native._auto_create_opencode_terminal(
            "role-test", None, lambda *_: None, agent_spec=_spec()
        )
    assert native._AUTO_OPENCODE_SERVERS["role-test"] is cached
    cached.close.assert_not_awaited()
    cancel.assert_not_awaited()


@pytest.mark.parametrize("role", ["unknown", "", False, {}, None])
def test_invalid_role_is_not_silently_defaulted(tmp_path: Path, role: object) -> None:
    with pytest.raises(ValueError, match="Unknown OpenCode tool role"):
        native._opencode_native_tool_role(_spec(role), _launch(tmp_path))


def test_other_roles_preserve_existing_launch_behavior(tmp_path: Path) -> None:
    assert (
        native._opencode_native_tool_role(None, _launch(tmp_path, external_session_id="existing"))
        is None
    )
    spec = _spec()
    spec.executor.config.clear()
    assert (
        native._opencode_native_tool_role(spec, _launch(tmp_path, external_session_id="existing"))
        is None
    )


@pytest.mark.parametrize("opt_in", [True, False])
async def test_role_config_is_written_before_server_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opt_in: bool
) -> None:
    from omnigent import opencode_native_app_server as server_module
    from omnigent import opencode_native_bridge as bridge_module
    from omnigent import opencode_native_provider as provider_module

    class Started(Exception):
        pass

    seen = {}

    class Server:
        def __init__(self, *, bridge_dir, workspace, extra_env):
            self.bridge_dir = bridge_dir
            seen["env"] = extra_env

        async def start(self):
            path = (
                bridge_module.xdg_config_home_for_bridge_dir(self.bridge_dir)
                / "opencode"
                / "opencode.json"
            )
            seen["config"] = json.loads(path.read_text())
            raise Started

    monkeypatch.setattr(server_module, "OpenCodeNativeServer", Server)
    monkeypatch.setattr(bridge_module, "prepare_bridge_dir", lambda _: tmp_path)
    monkeypatch.setattr(bridge_module, "seed_opencode_auth", lambda _: None)
    monkeypatch.setattr(bridge_module, "read_owned_gemma_auth_snapshot", lambda _: None)
    monkeypatch.setattr(
        native, "_opencode_native_launch_config", AsyncMock(return_value=_launch(tmp_path))
    )
    monkeypatch.setattr(
        native, "_opencode_native_model_from_spec", lambda _: OPENCODE_OWNED_GEMMA_MODEL
    )
    monkeypatch.setattr(native, "_cancel_auto_forwarder_task", AsyncMock())
    monkeypatch.setattr(provider_module, "resolve_databricks_gateway", lambda *_, **__: None)
    monkeypatch.setattr(provider_module, "maybe_merge_user_provider_config", lambda config: config)
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)
    spec = _spec()
    if not opt_in:
        spec.executor.config.clear()
    relay = AsyncMock()
    with pytest.raises(Started):
        await native._auto_create_opencode_terminal(
            "new-role",
            None,
            lambda *_: None,
            agent_spec=spec,
            server_client=SimpleNamespace(),
            ensure_comment_relay=relay,
        )
    relay.assert_awaited_once()
    assert seen["config"]["model"] == OPENCODE_OWNED_GEMMA_MODEL
    assert set(seen["config"]["mcp"]) == {"omnigent"}
    if not opt_in:
        assert seen["config"]["permission"] == "ask"
        assert seen["env"] is None
        return
    assert seen["config"]["permission"]["bash"] == "ask"
    assert seen["config"]["permission"]["*"] == "deny"
    assert (
        json.loads(seen["env"]["OPENCODE_CONFIG_CONTENT"])["permission"]
        == seen["config"]["permission"]
    )


@pytest.mark.parametrize("field,value", [("profile", "gateway"), ("mcp_servers", [object()])])
def test_ambiguous_role_spec_fails_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    spec = _spec()
    if field == "profile":
        spec.executor.config[field] = value
    else:
        spec.mcp_servers = value
    monkeypatch.setattr(
        native, "_opencode_native_model_from_spec", lambda _: OPENCODE_OWNED_GEMMA_MODEL
    )
    with pytest.raises(ValueError, match="unambiguous"):
        native._opencode_native_tool_role(spec, _launch(tmp_path))


async def test_merged_unknown_plugin_preserves_cached_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import opencode_native_bridge as bridge_module
    from omnigent import opencode_native_provider as provider_module

    cached = SimpleNamespace(close=AsyncMock())
    monkeypatch.setitem(native._AUTO_OPENCODE_SERVERS, "plugin-test", cached)
    cancel = AsyncMock()
    monkeypatch.setattr(native, "_cancel_auto_forwarder_task", cancel)
    monkeypatch.setattr(
        native, "_opencode_native_launch_config", AsyncMock(return_value=_launch(tmp_path))
    )
    monkeypatch.setattr(
        native, "_opencode_native_model_from_spec", lambda _: OPENCODE_OWNED_GEMMA_MODEL
    )
    monkeypatch.setattr(bridge_module, "prepare_bridge_dir", lambda _: tmp_path)
    monkeypatch.setattr(provider_module, "resolve_databricks_gateway", lambda *_, **__: None)
    monkeypatch.setattr(
        provider_module,
        "maybe_merge_user_provider_config",
        lambda config: {**config, "plugin": ["unknown.js"]},
    )
    with pytest.raises(ValueError, match="cached native instance"):
        await native._auto_create_opencode_terminal(
            "plugin-test", None, lambda *_: None, agent_spec=_spec()
        )
    cached.close.assert_not_awaited()
    cancel.assert_not_awaited()
    assert native._AUTO_OPENCODE_SERVERS["plugin-test"] is cached


def test_inherited_permission_and_config_paths_cannot_override_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.opencode_native_app_server import filtered_server_env

    monkeypatch.setenv("OPENCODE_PERMISSION", '{"*":"allow","bash":"allow"}')
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/ambient/config")
    monkeypatch.setenv("OPENCODE_TEST_HOME", "/ambient/home")
    monkeypatch.setenv("OPENCODE_TEST_MANAGED_CONFIG_DIR", "/ambient/managed")
    config, extra = project_owned_gemma_tool_config(
        {"model": OPENCODE_OWNED_GEMMA_MODEL, "permission": "ask"}, bridge_dir=tmp_path
    )
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="fixture", extra_env=extra)
    assert json.loads(env["OPENCODE_PERMISSION"]) == config["permission"]
    assert env["OPENCODE_CONFIG_DIR"] == ""
    assert env["HOME"] == env["OPENCODE_TEST_HOME"] == str(tmp_path / "owned-role-home")
    assert env["OPENCODE_TEST_MANAGED_CONFIG_DIR"] == ""
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "true"
    ordinary = filtered_server_env(bridge_dir=tmp_path, auth_secret="fixture")
    assert ordinary["OPENCODE_CONFIG_DIR"] == "/ambient/config"
    assert json.loads(ordinary["OPENCODE_PERMISSION"])["*"] == "allow"


@pytest.mark.parametrize("field", ["agent", "default_agent", "tools", "mode"])
def test_future_agent_or_tool_config_producers_fail_closed(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError, match="additional config producers"):
        project_owned_gemma_tool_config(
            {"model": OPENCODE_OWNED_GEMMA_MODEL, field: {}}, bridge_dir=tmp_path
        )


def test_existing_home_plugins_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "owned-role-home" / ".opencode").mkdir(parents=True)
    with pytest.raises(ValueError, match="isolated config home"):
        project_owned_gemma_tool_config({"model": OPENCODE_OWNED_GEMMA_MODEL}, bridge_dir=tmp_path)


async def test_managed_config_context_preserves_cached_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda path: str(path) == "/etc/opencode" or original_exists(path)
    )
    cached = SimpleNamespace(close=AsyncMock())
    monkeypatch.setitem(native._AUTO_OPENCODE_SERVERS, "managed-test", cached)
    monkeypatch.setattr(
        native, "_opencode_native_launch_config", AsyncMock(return_value=_launch(tmp_path))
    )
    with pytest.raises(ValueError, match="unmanaged Linux"):
        await native._auto_create_opencode_terminal(
            "managed-test", None, lambda *_: None, agent_spec=_spec()
        )
    cached.close.assert_not_awaited()
    assert native._AUTO_OPENCODE_SERVERS["managed-test"] is cached


def test_real_agent_spec_binds_explicit_role_and_model(tmp_path: Path) -> None:
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(
            model=OPENCODE_OWNED_GEMMA_MODEL,
            config={"harness": "opencode-native", "opencode_tool_role": OPENCODE_OWNED_GEMMA_ROLE},
        ),
    )
    assert native._opencode_native_tool_role(spec, _launch(tmp_path)) == OPENCODE_OWNED_GEMMA_ROLE
    spec.executor.model = None
    with pytest.raises(ValueError, match="exact provider/model"):
        native._opencode_native_tool_role(spec, _launch(tmp_path))


@pytest.mark.parametrize(
    "auth",
    [{"server": {"type": "wellknown", "token": "fixture"}}, {"server": {"type": "unknown"}}, []],
)
def test_remote_or_unknown_auth_config_rejected_without_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: object
) -> None:
    from omnigent import opencode_native_bridge as bridge_module

    source = tmp_path / "source-auth.json"
    source.write_text(json.dumps(auth))
    monkeypatch.setattr(bridge_module, "user_opencode_auth_path", lambda: source)
    with pytest.raises(ValueError, match="remote or unknown auth"):
        bridge_module.read_owned_gemma_auth_snapshot(tmp_path / "bridge")
    assert not (tmp_path / "bridge").exists()


def test_auth_seed_uses_validated_snapshot_and_private_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import opencode_native_bridge as bridge_module

    source = tmp_path / "source-auth.json"
    source.write_text('{"p16v":{"type":"api","key":"fixture"}}')
    monkeypatch.setattr(bridge_module, "user_opencode_auth_path", lambda: source)
    bridge = tmp_path / "bridge"
    snapshot = bridge_module.read_owned_gemma_auth_snapshot(bridge)
    source.write_text('{"remote":{"type":"wellknown","token":"new-fixture"}}')
    seeded = bridge_module.seed_owned_gemma_auth(bridge, snapshot=snapshot)
    assert seeded.read_bytes() == snapshot
    assert seeded.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("directory", ["xdg-config", "xdg-data"])
def test_existing_config_or_account_state_rejected(tmp_path: Path, directory: str) -> None:
    from omnigent import opencode_native_bridge as bridge_module

    state = tmp_path / directory
    state.mkdir()
    (state / "fixture.json").write_text("{}")
    with pytest.raises(ValueError, match="fresh native config/account state"):
        bridge_module.read_owned_gemma_auth_snapshot(tmp_path)


async def test_valid_role_cannot_replace_cached_native_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = SimpleNamespace(close=AsyncMock())
    monkeypatch.setitem(native._AUTO_OPENCODE_SERVERS, "cached-role-test", cached)
    monkeypatch.setattr(
        native, "_opencode_native_launch_config", AsyncMock(return_value=_launch(tmp_path))
    )
    monkeypatch.setattr(
        native, "_opencode_native_model_from_spec", lambda _: OPENCODE_OWNED_GEMMA_MODEL
    )
    with pytest.raises(ValueError, match="cached native instance"):
        await native._auto_create_opencode_terminal(
            "cached-role-test", None, lambda *_: None, agent_spec=_spec()
        )
    cached.close.assert_not_awaited()


async def test_remote_auth_is_rejected_before_relay_or_server_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import opencode_native_bridge as bridge_module

    source = tmp_path / "source-auth.json"
    source.write_text('{"remote":{"type":"wellknown","token":"fixture"}}')
    monkeypatch.setattr(bridge_module, "user_opencode_auth_path", lambda: source)
    monkeypatch.setattr(bridge_module, "prepare_bridge_dir", lambda _: tmp_path / "bridge")
    relay_config = []
    monkeypatch.setattr(
        bridge_module, "write_relay_bridge_config", lambda _: relay_config.append(True)
    )
    cancel = AsyncMock()
    monkeypatch.setattr(native, "_cancel_auto_forwarder_task", cancel)
    monkeypatch.setattr(
        native, "_opencode_native_launch_config", AsyncMock(return_value=_launch(tmp_path))
    )
    monkeypatch.setattr(
        native, "_opencode_native_model_from_spec", lambda _: OPENCODE_OWNED_GEMMA_MODEL
    )
    with pytest.raises(ValueError, match="remote or unknown auth"):
        await native._auto_create_opencode_terminal(
            "remote-auth-test", None, lambda *_: None, agent_spec=_spec()
        )
    cancel.assert_not_awaited()
    assert not relay_config
    assert not (tmp_path / "bridge").exists()
