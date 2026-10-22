"""Native launch cwd must follow session wiring, never an empty normalized path."""

from __future__ import annotations

import contextlib
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from omnigent.host import connect as host_connect
from omnigent.onboarding import provider_config
from omnigent.runner._entry import _runner_workspace_from_env
from omnigent.runner.native import orchestration

_WORKSPACE_READERS = (
    orchestration._codex_session_workspace,
    orchestration._pi_session_workspace,
    orchestration._kiro_session_workspace,
)


@pytest.mark.parametrize("reader", _WORKSPACE_READERS, ids=["codex", "pi", "kiro"])
@pytest.mark.parametrize("snapshot", [None, " \t\n"])
def test_blank_workspace_refuses_inherited_process_cwd(reader, snapshot, tmp_path, monkeypatch):
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    monkeypatch.chdir(inherited)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", " \t\n")

    with pytest.raises(RuntimeError, match="empty after normalization"):
        reader(snapshot)


@pytest.mark.parametrize("reader", _WORKSPACE_READERS, ids=["codex", "pi", "kiro"])
def test_stored_worktree_beats_fallback_and_bundle_extraction(reader, tmp_path, monkeypatch):
    selected = tmp_path / "selected-worktree"
    selected.mkdir()
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    extraction = tmp_path / "bundle-extraction"
    extraction.mkdir()
    monkeypatch.chdir(extraction)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(fallback))

    assert reader("  " + str(selected) + "  ") == selected
    assert reader(None) == fallback


@pytest.mark.parametrize("reader", _WORKSPACE_READERS, ids=["codex", "pi", "kiro"])
def test_stored_worktree_symlink_is_canonical(reader, tmp_path, monkeypatch):
    selected = tmp_path / "selected-worktree"
    selected.mkdir()
    link = tmp_path / "worktree-link"
    link.symlink_to(selected, target_is_directory=True)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "fallback"))

    assert reader("  " + str(link) + "  ") == selected


@pytest.mark.parametrize("reader", _WORKSPACE_READERS, ids=["codex", "pi", "kiro"])
def test_missing_snapshot_and_fallback_still_refuse(reader, monkeypatch):
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    with pytest.raises(RuntimeError, match="OMNIGENT_RUNNER_WORKSPACE must be set"):
        reader(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", [False, True])
async def test_complete_codex_snapshot_parser_uses_or_refuses_workspace(
    blank, tmp_path, monkeypatch
):
    selected = tmp_path / "selected-worktree"
    selected.mkdir()
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    monkeypatch.chdir(inherited)
    monkeypatch.setenv("RUNNER_SERVER_URL", "https://synthetic.invalid")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "fallback"))
    requests = []

    def response(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={"workspace": " \t" if blank else str(selected), "labels": {}},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(response), base_url="https://synthetic.invalid"
    ) as client:
        if blank:
            with pytest.raises(RuntimeError, match="empty after normalization"):
                await orchestration._codex_native_launch_config(
                    session_id="synthetic-session", server_client=client
                )
        else:
            result = await orchestration._codex_native_launch_config(
                session_id="synthetic-session", server_client=client
            )
            assert result.workspace == selected
    assert requests == ["/v1/sessions/synthetic-session"]


def test_entrypoint_retains_its_existing_blank_refusal(monkeypatch):
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "  ")
    with pytest.raises(RuntimeError, match="must not be empty"):
        _runner_workspace_from_env()


def test_real_host_environment_and_spawn_keep_selected_workspace(tmp_path, monkeypatch):
    selected = tmp_path / "selected-worktree"
    selected.mkdir()
    # This test must never read the developer's provider configuration.
    monkeypatch.setattr(provider_config, "load_config", dict)
    monkeypatch.setattr(provider_config, "provider_credential_env_vars", lambda _: frozenset())
    env = host_connect._build_runner_env(
        {},
        server_url="https://synthetic.invalid",
        runner_id="synthetic-runner",
        binding_token="synthetic-binding",
        workspace=str(selected),
        parent_pid=1,
    )
    assert env["OMNIGENT_RUNNER_WORKSPACE"] == str(selected)

    daemon = host_connect.HostProcess.__new__(host_connect.HostProcess)
    monkeypatch.setattr(daemon, "_ensure_zygote_started", lambda: None)
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=-1)

    # Replace only the owning module's reference, leaving global Popen intact.
    monkeypatch.setattr(
        host_connect, "subprocess", SimpleNamespace(Popen=fake_popen, DEVNULL=subprocess.DEVNULL)
    )
    log_path = tmp_path / "synthetic-log"
    log_handle = log_path.open("wb")
    monkeypatch.setattr(
        host_connect, "open_process_log_file", Mock(return_value=(log_path, log_handle))
    )
    monkeypatch.setattr(
        host_connect, "child_logging_popen_kwargs", lambda _: contextlib.nullcontext({})
    )
    proc, actual_log = daemon._spawn_runner_proc(env, "synthetic-", selected)

    assert proc.pid == -1 and actual_log == log_path
    assert len(calls) == 1
    assert calls[0][1]["cwd"] == str(selected)
    assert calls[0][1]["env"]["OMNIGENT_RUNNER_WORKSPACE"] == str(selected)
    assert calls[0][1]["stdin"] == subprocess.DEVNULL
    assert log_handle.closed
