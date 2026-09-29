"""Regression tests for the codex-native "thread already has an active writer" failure.

Codex guards each thread with an exclusive cross-process file lock under
``$CODEX_HOME/thread-writer-locks``. The runner used to replace a still-live
app-server whenever the auxiliary TUI pane had died: it SIGTERMed the old
app-server (which Codex treats as a graceful drain of the in-flight turn, while
the fleet launcher shim in front of it exited at once, so ``close()`` reported
success) and booted a new one whose ``thread/resume`` was then refused with
"already has an active writer" — failing the session every time a sub-agent
completion was injected mid-turn.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent import codex_native_app_server
from omnigent.codex_native_app_server import (
    CodexAppServerResponseError,
    CodexNativeAppServer,
    is_codex_active_writer_error,
)
from omnigent.codex_native_bridge import CodexNativeBridgeState, write_bridge_state
from omnigent.runner.native import orchestration

_THREAD = "01a0cc43-e48b-72f0-b0f7-6dd0969975c1"
_ACTIVE_WRITER = {"code": -32600, "message": f"thread {_THREAD} already has an active writer"}


class _ScriptedClient:
    """Fake app-server client whose ``thread/resume`` replays scripted outcomes."""

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = outcomes
        self.requests: list[str] = []
        self.closed = False

    async def connect(self) -> None:
        return None

    async def request(self, method: str, _params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(method)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return {}

    async def close(self) -> None:
        self.closed = True


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _ScriptedClient) -> None:
    monkeypatch.setattr(codex_native_app_server, "client_for_transport", lambda *_a, **_k: client)


def test_is_codex_active_writer_error_matches_only_the_writer_conflict() -> None:
    assert is_codex_active_writer_error(CodexAppServerResponseError(_ACTIVE_WRITER))
    assert not is_codex_active_writer_error(
        CodexAppServerResponseError({"code": -32600, "message": "thread not found"})
    )


def test_preload_waits_for_previous_writer_to_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """A draining previous writer delays the preload instead of failing it."""
    client = _ScriptedClient(
        [
            CodexAppServerResponseError(_ACTIVE_WRITER),
            CodexAppServerResponseError(_ACTIVE_WRITER),
            None,
        ]
    )
    _install_client(monkeypatch, client)
    sleeps: list[float] = []

    async def _fast_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(codex_native_app_server.asyncio, "sleep", _fast_sleep)

    asyncio.run(
        codex_native_app_server.preload_codex_thread_for_resume(
            "ws://127.0.0.1:1", _THREAD, active_writer_wait_s=30.0
        )
    )

    assert client.requests == ["thread/resume"] * 3
    assert len(sleeps) == 2
    assert client.closed is True


def test_preload_gives_up_after_writer_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ScriptedClient([CodexAppServerResponseError(_ACTIVE_WRITER)] * 50)
    _install_client(monkeypatch, client)

    with pytest.raises(CodexAppServerResponseError, match="active writer"):
        asyncio.run(
            codex_native_app_server.preload_codex_thread_for_resume(
                "ws://127.0.0.1:1", _THREAD, active_writer_wait_s=0.0
            )
        )
    assert client.requests == ["thread/resume"]
    assert client.closed is True


def test_preload_does_not_retry_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ScriptedClient(
        [CodexAppServerResponseError({"code": -32600, "message": "thread not found"})]
    )
    _install_client(monkeypatch, client)

    with pytest.raises(CodexAppServerResponseError, match="not found"):
        asyncio.run(
            codex_native_app_server.preload_codex_thread_for_resume(
                "ws://127.0.0.1:1", _THREAD, active_writer_wait_s=30.0
            )
        )
    assert client.requests == ["thread/resume"]


# A launcher shim that dies on SIGTERM (Python's default) while its child — the
# stand-in for ``codex app-server`` draining a turn — ignores SIGTERM. Both
# share the launcher's process group, exactly like the fleet wrapper.
_LAUNCHER = """
import subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c",
     "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
     "open(sys.argv[1], 'w').write('ready'); time.sleep(120)",
     sys.argv[1]],
)
open(sys.argv[2], "w").write(str(child.pid))
child.wait()
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A reparented zombie still answers kill(0); treat it as gone.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


@pytest.mark.skipif(os.name != "posix" or not Path("/proc").is_dir(), reason="POSIX /proc")
def test_close_kills_draining_app_server_behind_exiting_launcher(tmp_path: Path) -> None:
    """``close()`` must not report success while the real app-server still runs."""
    ready = tmp_path / "ready"
    child_pid_file = tmp_path / "child.pid"

    async def _run() -> int:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _LAUNCHER,
            str(ready),
            str(child_pid_file),
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while not (ready.exists() and child_pid_file.exists()):
            assert time.monotonic() < deadline, "launcher never started its child"
            await asyncio.sleep(0.05)
        child_pid = int(child_pid_file.read_text())
        server = CodexNativeAppServer(
            codex_path="codex",
            socket_path=tmp_path / "sock",
            codex_home=tmp_path / "home",
            env={},
            config_overrides=[],
            cwd=tmp_path,
            bridge_dir=tmp_path,
            request_session_id="conv_test",
            proc=proc,
            process_group_id=os.getpgid(proc.pid),
        )
        await server.close()
        return child_pid

    child_pid = asyncio.run(_run())
    try:
        assert not _pid_alive(child_pid)
    finally:
        if _pid_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


class _LiveProc:
    returncode: int | None = None


class _FakeAppServer:
    def __init__(self, listen_url: str) -> None:
        self.listen_url = listen_url
        self.proc = _LiveProc()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _launch_config(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "workspace": Path("/work"),
        "policy_server_url": "http://127.0.0.1:1",
        "terminal_launch_args": ["--model", "gpt"],
        "model_override": None,
        "external_session_id": _THREAD,
        "fork_source_id": None,
        "fork_source_external_id": None,
        "fork_carry_history": False,
        "bypass_sandbox": True,
    }
    values.update(overrides)
    return orchestration._CodexNativeLaunchConfig(**values)


@pytest.fixture
def live_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Register a live app-server + forwarder + bridge state for ``conv_live``."""
    session_id = "conv_live"
    ws_url = "ws://127.0.0.1:4242"
    server = _FakeAppServer(ws_url)
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id=session_id,
            socket_path=ws_url,
            thread_id=_THREAD,
            codex_home=str(tmp_path / "codex-home"),
            cwd="/work",
        ),
    )

    class _RunningTask:
        def done(self) -> bool:
            return False

    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, session_id, server)
    monkeypatch.setitem(orchestration._AUTO_FORWARDER_TASKS, session_id, _RunningTask())
    monkeypatch.setitem(
        orchestration._AUTO_CODEX_LAUNCH_FINGERPRINTS,
        session_id,
        (server, orchestration._codex_launch_fingerprint(_launch_config())),
    )
    return session_id, server, ws_url, tmp_path


def test_live_app_server_is_reused_for_a_dead_tui(live_session: Any) -> None:
    session_id, server, ws_url, bridge_dir = live_session
    live = orchestration._live_codex_app_server_for_relaunch(
        session_id, _launch_config(), bridge_dir
    )
    assert live == (server, ws_url, _THREAD)


@pytest.mark.parametrize(
    "change",
    [
        {"model_override": "gpt-other"},
        {"terminal_launch_args": ["--model", "other"]},
        {"bypass_sandbox": False},
        {"external_session_id": "019e0000-0000-7000-8000-000000000000"},
    ],
)
def test_changed_launch_settings_force_full_relaunch(
    live_session: Any, change: dict[str, Any]
) -> None:
    session_id, _server, _ws_url, bridge_dir = live_session
    assert (
        orchestration._live_codex_app_server_for_relaunch(
            session_id, _launch_config(**change), bridge_dir
        )
        is None
    )


def test_exited_app_server_or_forwarder_is_not_reused(
    live_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, server, _ws_url, bridge_dir = live_session
    server.proc.returncode = 0
    assert (
        orchestration._live_codex_app_server_for_relaunch(session_id, _launch_config(), bridge_dir)
        is None
    )
    server.proc.returncode = None

    class _DoneTask:
        def done(self) -> bool:
            return True

    monkeypatch.setitem(orchestration._AUTO_FORWARDER_TASKS, session_id, _DoneTask())
    assert (
        orchestration._live_codex_app_server_for_relaunch(session_id, _launch_config(), bridge_dir)
        is None
    )


def test_auto_create_relaunches_only_the_tui_on_a_live_app_server(
    live_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ensure path must not cancel the forwarder or replace a live app-server."""
    session_id, server, ws_url, bridge_dir = live_session

    async def _launch_config_for(**_kwargs: Any) -> Any:
        return _launch_config()

    async def _forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("live app-server must not be torn down")

    launched: dict[str, Any] = {}

    async def _fake_tui(**kwargs: Any) -> str:
        launched.update(kwargs)
        return "terminal-view"

    monkeypatch.setattr(orchestration, "_codex_native_launch_config", _launch_config_for)
    monkeypatch.setattr(orchestration, "_cancel_auto_forwarder_task", _forbidden)
    monkeypatch.setattr(orchestration, "_launch_codex_tui_terminal", _fake_tui)
    monkeypatch.setattr("omnigent.codex_native_bridge.prepare_bridge_dir", lambda _sid: bridge_dir)
    view = asyncio.run(
        orchestration._auto_create_codex_terminal(
            session_id,
            resource_registry=object(),  # type: ignore[arg-type]
            publish_event=lambda *_a: None,
        )
    )

    assert view == "terminal-view"
    assert launched["app_server"] is server
    assert launched["codex_ws_url"] == ws_url
    assert launched["thread_id"] == _THREAD
    assert server.closed is False
    assert orchestration._AUTO_CODEX_APP_SERVERS[session_id] is server
