"""review129 probes for oz/omnigent#129 (terminal-UI Codex llmq hook, phase 3b).

Each test states the defect it demonstrates. They reuse the PR's own ``World``
harness (scripted llmq + Omnigent server, the hook driven through ``main`` the way
Codex runs it).

Real-Codex evidence for the steer probes (codex-cli 0.159.1, fake llmq/provider,
``review129/exp/goal_exp.py`` and ``steer-run1.txt``): a ``turn/steer`` fires
``UserPromptSubmit`` carrying the RUNNING turn's ``turn_id`` when the steered input
is consumed, and a goal-mode continuation turn (started by Codex itself, admitted by
the forwarder's goal gate) that is steered got a second admission from this hook.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import codex_llmq_hook
from omnigent.codex_native_bridge import write_policy_hook_config
from tests.test_codex_llmq_hook import (
    LLMQ_TOKEN,
    SERVER_TOKEN,
    SESSION,
    World,
    _gate_env,
)

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


class _GoalCodex:
    """The app-server as the goal gate sees it: the thread's goal is active."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/goal/get":
            return {"id": 1, "result": {"goal": {"objective": "ship it", "status": "active"}}}
        return {"id": 2, "result": {}}


def _steer(turn_id: str) -> dict[str, Any]:
    """The UserPromptSubmit Codex fires when a steer's input is consumed."""
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "codex-thread-1",
        "turn_id": turn_id,
        "model": "gpt-6.1-sol",
        "prompt": "steer: also do the other thing",
        "cwd": "/somewhere",
        "transcript_path": "/somewhere/rollout.jsonl",
        "permission_mode": "default",
    }


def _admissions(world: World) -> int:
    return world.llmq_paths().count("admissions")


# --------------------------------------------------------------- D1: steer double admission


async def test_steer_into_a_goal_continuation_turn_is_not_admitted_twice(world: World) -> None:
    """D1a. Codex goal mode starts a continuation turn by itself; the forwarder's goal
    gate admits it at ``turn/started``. A steer into that running turn (typed in the
    TUI, or a web message the runner delivers as ``turn/steer``) fires
    ``UserPromptSubmit`` with the continuation's turn id, which the hook has never
    seen: it admits (and activates, and later settles) a second reservation for the
    same Codex turn. Expected: one admission for one turn.
    """
    from omnigent.runner.llmq_gate import GateConfig
    from omnigent.runner.llmq_goal_gate import GoalContinuationGate

    config = GateConfig.from_env(_gate_env(world.token_file))
    assert config is not None
    gate = GoalContinuationGate(config, attribution_wait_seconds=0.0)
    async with httpx.AsyncClient(
        base_url="http://omni.test", headers={"Authorization": f"Bearer {SERVER_TOKEN}"}
    ) as ap:
        gate.turn_started(
            session_id=SESSION,
            thread_id="thread_1",
            turn_id="turn-goal-1",
            bridge_dir=world.bridge,
            server=ap,
            codex=_GoalCodex(),
            model="gpt-6.1-sol",
        )
        await gate.drain()
        assert _admissions(world) == 1  # the goal gate admitted the continuation
        out = await asyncio.to_thread(world.run, "prompt-submit", _steer("turn-goal-1"))
        assert json.loads(out) == {}
        gate.turn_finished("turn-goal-1")
        await gate.drain()
        await gate.aclose()
    assert _admissions(world) == 1, world.llmq_paths()


def test_runner_steer_into_an_unverified_typed_turn_is_not_admitted_twice(
    world: World,
) -> None:
    """D1b. A typed prompt admitted UNVERIFIED (llmq answered 503) has no reservation,
    so the hook keeps no ``open`` turn id. A web message the runner then delivers
    into the running turn was already admitted by the runner gate
    (``runner/app.py`` gates every delivered message) and is sent as ``turn/steer``,
    which writes no runner marker. Its ``UserPromptSubmit`` (same turn id) is
    admitted again by the hook. Expected: the hook skips a turn it already admitted.
    """
    world.admission = lambda r: httpx.Response(503, text="down")
    assert json.loads(world.run()) == {}
    assert _admissions(world) == 1
    world.admission = lambda r: httpx.Response(
        200,
        json={
            "allowed": True,
            "reason": "admitted",
            "verified": True,
            "reservation_id": "res-tui-00000002",
        },
    )
    assert json.loads(world.run("prompt-submit", _steer("turn-1"))) == {}
    assert _admissions(world) == 1, world.llmq_paths()


# --------------------------------------------------------------- D3: cancel after activation


class _ReadyWorld(World):
    """llmq answers activations the way the real app does (``ready: true``)."""

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "llmq.test" and request.url.path.endswith("/activations"):
            body = json.loads(request.content)
            self.calls.append((request.url.host, request.url.path, body))
            return httpx.Response(
                200, json={"reservation_id": body["reservation_id"], "ready": True}
            )
        return super()._handle(request)


def test_an_activated_turn_is_never_cancelled_even_by_an_old_llmq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3 (Low). The hook activates every verified reservation at admission (llmq
    then records ``dispatch_authorized_at``), but ``_settle_open`` always calls
    ``close_codex_turn(activated=False)``. Against an llmq without
    ``usage_unmeasured`` (422) it therefore sends a cancellation for a turn that
    RAN, which the mandate forbids ("NEVER cancel for a turn that ran"; llmq answers
    409). The runner and goal gate pass ``activated=`` (``activate_codex_turn``'s
    ``ready``) and get ``"retained"`` instead.
    """
    world = _ReadyWorld(tmp_path, monkeypatch)
    world.settlement = lambda r: httpx.Response(422, json={"detail": "extra field"})
    world.run()
    assert "activations" in world.llmq_paths()
    world.run("stop", {"hook_event_name": "Stop", "turn_id": "turn-1"})
    assert "cancellations" not in world.llmq_paths(), world.llmq_paths()


# --------------------------------------------------------------- D4: fail-open latency


def _hung_listener() -> tuple[socket.socket, list[socket.socket]]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    connections: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            connections.append(conn)  # never answered

    threading.Thread(target=accept, daemon=True).start()
    return listener, connections


@pytest.mark.parametrize(
    ("server_hung", "bound_s"),
    [(False, 9.0), (True, 14.0)],
    ids=["llmq-hung", "llmq+server-hung"],
)
def test_a_hung_llmq_fails_open_within_one_timeout_even_after_an_interrupted_turn(
    world: World, server_hung: bool, bound_s: float
) -> None:
    """D4 (Low). Codex fires no ``Stop`` for an interrupted turn (real Codex 0.159.1,
    ``review129/exp/interrupt-run1.txt``), so the next prompt starts with an
    ``open`` reservation and settles it ON the prompt path before asking llmq. With
    llmq hung, the typed prompt waits settle 5 s + admission 5 s; with the Omnigent
    server hung too, + Turbo GET 5 s + indicator POST 5 s (20 s). The PR's own bound
    for "5 s, no retry" is <= 9 s (``test_hung_controller_fails_open_after_five_seconds_
    without_retry``).
    """
    llmq, llmq_conns = _hung_listener()
    server, server_conns = _hung_listener()
    (world.bridge / "llmq_gate.json").write_text(
        json.dumps(_gate_env(world.token_file, url=f"http://127.0.0.1:{llmq.getsockname()[1]}"))
    )
    server_url = (
        f"http://127.0.0.1:{server.getsockname()[1]}" if server_hung else "http://127.0.0.1:1"
    )
    write_policy_hook_config(world.bridge, ap_server_url=server_url, ap_auth_headers={})
    # The previous typed turn was interrupted: its reservation is still open.
    (world.bridge / "llmq_gate_state.json").write_text(
        json.dumps(
            {
                "session_id": SESSION,
                "open": {
                    "codex_turn_id": "turn-0",
                    "turn_key": "a" * 64,
                    "reservation_id": "res-tui-00000000",
                },
            }
        )
    )
    world.monkeypatch.setattr(httpx, "AsyncClient", world.real_client)
    try:
        started = time.monotonic()
        out = world.run()
        elapsed = time.monotonic() - started
    finally:
        for sock in (llmq, server, *llmq_conns, *server_conns):
            sock.close()
    assert json.loads(out) == {}
    print(f"elapsed={elapsed:.2f}s llmq_conns={len(llmq_conns)} server_conns={len(server_conns)}")
    # llmq hung: one 5 s timeout (bound 9 s, the PR's own). Server hung too: the
    # Turbo GET (5 s) must precede the admission (5 s); the indicator POST to a
    # server that just timed out is skipped (bound 14 s; 20 s at c647ce6a).
    assert elapsed <= bound_s, f"typed prompt held {elapsed:.1f} s"


# --------------------------------------------------------------- guard rails (expected GREEN)


def test_review_guard_admitted_turns_file_name_matches_llmq_gate() -> None:
    from omnigent.runner.llmq_gate import ADMITTED_TURNS_FILE

    assert getattr(codex_llmq_hook, "_ADMITTED_TURNS_FILE", ADMITTED_TURNS_FILE) == (
        ADMITTED_TURNS_FILE
    )


def test_review_guard_a_recorded_runner_turn_still_takes_its_marker(world: World) -> None:
    """The executor records its turn id once ``turn/start`` returns, which can be
    before that turn's hook runs. The hook must still consume the runner marker,
    or the NEXT typed prompt would take it and run ungated.
    """
    from omnigent.runner.llmq_gate import note_admitted_turn

    assert codex_llmq_hook.mark_runner_turn(world.bridge)
    note_admitted_turn(world.bridge, "turn-runner-1")
    assert json.loads(world.run("prompt-submit", _steer("turn-runner-1"))) == {}
    assert world.llmq_paths() == []
    assert not (world.bridge / "llmq_runner_turn.json").exists()
    # The next typed prompt (a new turn) is gated.
    assert json.loads(world.run("prompt-submit", _steer("turn-typed-2"))) == {}
    assert world.llmq_paths().count("admissions") == 1


def test_steer_into_a_goal_continuation_while_a_typed_turn_is_open(world: World) -> None:
    """Heavy path of D1a: an interrupted typed turn left its reservation open
    (Codex fires no Stop on interrupt); the steer into the goal continuation
    settles it and is not admitted itself.
    """
    from omnigent.runner.llmq_gate import note_admitted_turn

    world.run()  # typed turn-1 admitted; then interrupted: no Stop
    assert world.state()["open"]["codex_turn_id"] == "turn-1"
    note_admitted_turn(world.bridge, "turn-goal-1", clear_pending=False)  # goal gate's record
    assert json.loads(world.run("prompt-submit", _steer("turn-goal-1"))) == {}
    assert world.llmq_paths() == ["admissions", "activations", "settlements"]
    assert "open" not in world.state()


def test_review_guard_inert_hook_reads_nothing_and_prints_only_empty_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No snapshot, no gate env: ``{}`` and no file is created in the bridge dir."""
    import io
    import sys

    for name in codex_llmq_hook.GATE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    bridge = tmp_path / "b"
    bridge.mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert codex_llmq_hook.main(["prompt-submit", "--bridge-dir", str(bridge)]) == 0
    assert out.getvalue() == "{}"
    assert list(bridge.iterdir()) == []
    assert LLMQ_TOKEN not in out.getvalue()
