"""llmq admission for Codex goal-mode continuation turns (review D3b; needs llmq).

Codex goal mode starts continuation turns by itself, with no runner turn/start, so
the runner's pre-turn gate never saw them. The codex-native forwarder now hands
every ``turn/started`` to :class:`GoalContinuationGate`, which admits a turn nobody
admitted beforehand while the thread's goal is active, through llmq main's real
app (ASGI transport). Proves:

- below the margin the continuation runs; its reservation is activated and settled
  with the turn's measured usage (summed over its model responses);
- at the margin with Turbo off it is held: goal paused (objective untouched), turn
  interrupted, held status with resets_at and the Turbo hint; re-checked with fresh
  attempts; a live Turbo toggle resumes the goal once, and the continuation Codex
  starts then runs on that admission (no second admission);
- faults and undecidable answers let it run with the unverified indicator; a goal
  that cannot be paused is not interrupted (``ungated_continuation``);
- turns the runner admitted (recorded by the codex-native executor) and turns
  without an active goal are left alone;
- the forwarder wires turn/started, token usage and turn/completed into the gate.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("llm_quota_controller.api")

from omnigent import codex_native_forwarder as fwd
from omnigent.runner import llmq_goal_gate as goal_gate_module
from omnigent.runner.llmq_gate import (
    GateConfig,
    mark_turn_start_pending,
    note_admitted_turn,
)
from omnigent.runner.llmq_goal_gate import GoalContinuationGate
from tests.runner.test_llmq_gate_contract_llmq_main import ACTOR, UNMEASURED_FORM, Llmq

THREAD = "thread-goal"
SESSION = "conv_goal"


class FakeCodex:
    """The app-server, as far as the gate talks to it: goal state and interrupts."""

    def __init__(self, goal_status: str | None = "active", *, fail: set[str] | None = None):
        self.goal_status = goal_status
        self.fail = fail or set()
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method in self.fail:
            raise RuntimeError(f"{method} unsupported")
        if method == "thread/goal/get":
            goal = (
                None
                if self.goal_status is None
                else {"threadId": THREAD, "objective": "ship it", "status": self.goal_status}
            )
            return {"id": 1, "result": {"goal": goal}}
        if method == "thread/goal/set":
            self.goal_status = params.get("status", self.goal_status)
            return {"id": 2, "result": {"goal": {"status": self.goal_status}}}
        return {"id": 3, "result": {}}

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.requests if name == method]


class FakeServer:
    """The Omnigent server: the session's live Turbo flag, and status posts."""

    def __init__(self) -> None:
        self.turbo = False
        self.gate_posts: list[dict[str, Any]] = []

    def client(self) -> httpx.AsyncClient:
        async def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == f"/v1/sessions/{SESSION}":
                return httpx.Response(200, json={"turbo": self.turbo})
            if request.method == "POST":
                body = json.loads(request.content or b"{}")
                data = body.get("data") if isinstance(body, dict) else None
                if isinstance(data, dict) and "quota_gate" in data:
                    self.gate_posts.append(data)
            return httpx.Response(200, json={})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://ap")


def usage(turn_id: str, last: tuple[int, int, int], total: tuple[int, int, int]) -> dict:
    def breakdown(values: tuple[int, int, int]) -> dict[str, int]:
        inp, cached, out = values
        return {
            "inputTokens": inp,
            "cachedInputTokens": cached,
            "outputTokens": out,
            "totalTokens": inp + out,
        }

    return {
        "threadId": THREAD,
        "turnId": turn_id,
        "tokenUsage": {"last": breakdown(last), "total": breakdown(total)},
    }


@pytest.fixture
def setup(tmp_path: Path):
    token = tmp_path / "token"
    token.write_text("agent-token\n")
    llmq = Llmq(tmp_path)
    config = GateConfig(
        controller_url="http://llmq.test", token_file=token, agent_id=ACTOR, recheck_seconds=30.0
    )
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    return llmq, config, bridge


def make_gate(llmq: Llmq, config: GateConfig, sleeps: list[float], on_sleep=None, **kwargs):
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if on_sleep is not None:
            await on_sleep(len(sleeps))
        await asyncio.sleep(0)

    return GoalContinuationGate(
        config,
        http=llmq.client(),
        sleep=sleep,
        attribution_wait_seconds=kwargs.pop("attribution_wait_seconds", 1.0),
        interrupt_settle_seconds=kwargs.pop("interrupt_settle_seconds", 0.5),
        **kwargs,
    )


def start(gate, codex, server_client, bridge: Path, turn_id: str) -> None:
    gate.turn_started(
        session_id=SESSION,
        thread_id=THREAD,
        turn_id=turn_id,
        bridge_dir=bridge,
        server=server_client,
        codex=codex,
        model="gpt-6.1-sol",
    )


def reservations(llmq: Llmq) -> list[Any]:
    with llmq.engine.db.connect() as connection:
        return connection.execute("SELECT * FROM reservations").fetchall()


async def test_a_continuation_below_the_margin_runs_and_settles_its_measured_usage(setup) -> None:
    llmq, config, bridge = setup
    llmq.publish(33)
    codex, server = FakeCodex(), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await gate.drain()
        gate.usage_updated(usage("t1", (1000, 0, 40), (1000, 0, 40)))
        gate.usage_updated(usage("t1", (1000, 0, 40), (1000, 0, 40)))  # re-sent: not new
        gate.usage_updated(usage("t1", (1200, 900, 60), (2200, 900, 100)))
        gate.turn_finished("t1")
        await gate.drain()
        await gate.aclose()
    rows = reservations(llmq)
    assert len(llmq.seen) == 1 and llmq.seen[0]["turbo"] is False
    assert len(rows) == 1 and rows[0]["status"] == "settled"
    metadata = json.loads(rows[0]["metadata_json"])
    assert metadata.get("dispatch_authorized_at") is not None  # activated: it was running
    assert (metadata["native_input_tokens"], metadata["native_output_tokens"]) == (2200, 100)
    assert metadata["native_cached_input_tokens"] == 900
    assert codex.calls("turn/interrupt") == [] and server.gate_posts == []


async def test_a_continuation_at_the_margin_is_held_until_turbo_resumes_the_goal(setup) -> None:
    llmq, config, bridge = setup
    quota = llmq.publish(95)
    codex, server = FakeCodex(), FakeServer()
    sleeps: list[float] = []

    async def on_sleep(n: int) -> None:
        if n == 2:
            server.turbo = True  # the user flips Turbo while the goal is held

    gate = make_gate(llmq, config, sleeps, on_sleep)
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await asyncio.sleep(0.05)
        gate.turn_finished("t1")  # Codex ends the interrupted turn
        for _ in range(200):
            if codex.goal_status == "active" and len(codex.calls("thread/goal/set")) == 2:
                break
            await asyncio.sleep(0.01)
        await gate.drain()
        # Codex starts the continuation again once the goal is active:
        start(gate, codex, ap, bridge, "t2")
        await gate.drain()
        gate.usage_updated(usage("t2", (500, 0, 20), (500, 0, 20)))
        gate.turn_finished("t2")
        await gate.drain()
        await gate.aclose()

    # Held: goal paused BEFORE the interrupt, the objective never touched.
    assert codex.calls("thread/goal/set") == [
        {"threadId": THREAD, "status": "paused"},
        {"threadId": THREAD, "status": "active"},
    ]
    assert codex.calls("turn/interrupt") == [{"threadId": THREAD, "turnId": "t1"}]
    names = [name for name, _ in codex.requests]
    assert names.index("thread/goal/set") < names.index("turn/interrupt")
    # Re-checked every 30 s with fresh attempts; Turbo read live; no second admission
    # for the resumed continuation.
    assert sleeps == [30.0, 30.0]
    assert [r["attempt"] for r in llmq.seen] == [0, 1, 2]
    assert [r["turbo"] for r in llmq.seen] == [False, False, True]
    assert len({r["turn_key"] for r in llmq.seen}) == 1
    states = [p["quota_gate"]["state"] for p in server.gate_posts]
    assert states == ["blocked", "blocked", "verified"]
    held = server.gate_posts[0]
    assert held["quota_gate"]["reason"] == "credit_safety_margin"
    assert (
        held["quota_gate"]["resets_at"]
        == quota["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"]
    )
    assert "Enable Turbo" in held["blocked_on"] and "resets at" in held["blocked_on"]
    assert held["status"] == "running"  # like a held runner turn: the sentence shows
    assert "blocked_on" not in server.gate_posts[-1]
    # Never an extra idle edge (it would read as a native sub-agent's turn end).
    assert {p["status"] for p in server.gate_posts} == {"running"}
    # The continuation ran on the Turbo admission: credit-bearing, activated, closed.
    rows = reservations(llmq)
    assert len(rows) == 1
    metadata = json.loads(rows[0]["metadata_json"])
    assert metadata.get("native_credit_bearing") is True
    assert metadata.get("dispatch_authorized_at") is not None
    assert rows[0]["status"] == "settled"
    assert (metadata["native_input_tokens"], metadata["native_output_tokens"]) == (500, 20)


async def test_the_user_resuming_a_held_goal_ends_the_hold(setup) -> None:
    llmq, config, bridge = setup
    llmq.publish(95)
    codex, server = FakeCodex(), FakeServer()

    async def on_sleep(n: int) -> None:
        codex.goal_status = "active"  # resumed from the goal UI while held

    gate = make_gate(llmq, config, [], on_sleep)
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await asyncio.sleep(0.05)
        gate.turn_finished("t1")
        await gate.drain()  # the refusal started the hold
        for _ in range(100):
            if not gate._waits:
                break
            await asyncio.sleep(0.01)
        assert not gate._waits  # the hold ended on its own
        await gate.aclose()
    assert len(llmq.seen) == 1  # no re-check admission after the user took over
    assert codex.calls("thread/goal/set") == [{"threadId": THREAD, "status": "paused"}]


async def test_a_goal_that_cannot_be_paused_is_not_interrupted(setup) -> None:
    """Interrupting without a pause would only make Codex start the next
    continuation at once: the turn runs, and the session says it ran ungated."""
    llmq, config, bridge = setup
    llmq.publish(95)
    codex, server = FakeCodex(fail={"thread/goal/set"}), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await gate.drain()
        await gate.aclose()
    assert codex.calls("turn/interrupt") == []
    assert [(p["quota_gate"]["state"], p["quota_gate"]["reason"]) for p in server.gate_posts] == [
        ("unverified", "ungated_continuation")
    ]


@pytest.mark.parametrize(
    ("prepare", "slug"),
    [
        (lambda q: (q.publish(95), q.receipt.unlink()), "receipt_missing"),
        (lambda q: q.publish(95, age=301), "receipt_stale"),
    ],
)
async def test_undecidable_evidence_lets_the_continuation_run_with_the_indicator(
    setup, prepare, slug
) -> None:
    llmq, config, bridge = setup
    prepare(llmq)
    codex, server = FakeCodex(), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await gate.drain()
        await gate.aclose()
    assert codex.calls("turn/interrupt") == [] and codex.calls("thread/goal/set") == []
    assert [(p["quota_gate"]["state"], p["quota_gate"]["reason"]) for p in server.gate_posts] == [
        ("unverified", slug)
    ]
    assert reservations(llmq) == []


async def test_an_unreachable_llmq_lets_the_continuation_run_with_the_indicator(setup) -> None:
    _llmq, config, bridge = setup

    async def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    codex, server = FakeCodex(), FakeServer()
    gate = GoalContinuationGate(
        config, http=httpx.AsyncClient(transport=httpx.MockTransport(down))
    )
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await gate.drain()
        await gate.aclose()
    assert codex.calls("turn/interrupt") == []
    assert [(p["quota_gate"]["state"], p["quota_gate"]["reason"]) for p in server.gate_posts] == [
        ("unverified", "llmq_unreachable")
    ]


@pytest.mark.parametrize("how", ["recorded", "recorded-after-pending"])
async def test_a_turn_the_runner_admitted_is_not_admitted_again(setup, how) -> None:
    llmq, config, bridge = setup
    llmq.publish(95)
    codex, server = FakeCodex(), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        if how == "recorded":
            note_admitted_turn(bridge, "t-runner")
            start(gate, codex, ap, bridge, "t-runner")
        else:
            # turn/started can reach the forwarder before the executor has the
            # turn/start answer; the pending marker makes the gate wait for it.
            mark_turn_start_pending(bridge)
            start(gate, codex, ap, bridge, "t-runner")
            await asyncio.sleep(0.2)
            note_admitted_turn(bridge, "t-runner")
        await gate.drain()
        await gate.aclose()
    assert llmq.seen == [] and server.gate_posts == [] and codex.requests == []


@pytest.mark.parametrize("status", [None, "paused", "complete", "usageLimited"])
async def test_a_turn_without_an_active_goal_is_left_alone(setup, status) -> None:
    llmq, config, bridge = setup
    llmq.publish(95)
    codex, server = FakeCodex(goal_status=status), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await gate.drain()
        await gate.aclose()
    assert llmq.seen == [] and server.gate_posts == []
    assert [name for name, _ in codex.requests] == ["thread/goal/get"]


async def test_a_tracked_continuation_whose_end_was_missed_is_closed(setup) -> None:
    """A reconnect can lose turn/completed: the next turn/started, or the forwarder
    stopping, closes the reservation (unmeasured) instead of leaking its lease."""
    llmq, config, bridge = setup
    llmq.publish(33)
    codex, server = FakeCodex(), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await gate.drain()
        note_admitted_turn(bridge, "t2")
        start(gate, codex, ap, bridge, "t2")  # t1's end was never seen
        await gate.drain()
        await gate.aclose()
    rows = reservations(llmq)
    assert len(rows) == 1
    assert rows[0]["status"] == ("settled" if UNMEASURED_FORM else "active")
    assert "native_input_tokens" not in json.loads(rows[0]["metadata_json"])


async def test_the_forwarder_hands_turn_events_to_the_gate(tmp_path: Path) -> None:
    class Spy:
        def __init__(self) -> None:
            self.events: list[tuple[str, Any]] = []

        def turn_started(self, **kwargs: Any) -> None:
            self.events.append(("started", kwargs))

        def usage_updated(self, params: Any) -> None:
            self.events.append(("usage", params["turnId"]))

        def turn_finished(self, turn_id: str | None) -> None:
            self.events.append(("finished", turn_id))

    spy = Spy()
    codex = FakeCodex()
    state = fwd._CodexForwarderState(
        parent_session_id=SESSION,
        codex_client=codex,  # type: ignore[arg-type]
        llmq_goal_gate=spy,  # type: ignore[arg-type]
    )
    state.model = "gpt-6.1-sol"
    server = FakeServer()
    async with server.client() as ap:
        usage_coalescer = fwd._SessionUsageCoalescer(ap, SESSION)
        for event in (
            {
                "method": "turn/started",
                "params": {"threadId": THREAD, "turn": {"id": "t1", "status": "inProgress"}},
            },
            {"method": "thread/tokenUsage/updated", "params": usage("t1", (5, 0, 1), (5, 0, 1))},
            {
                "method": "turn/completed",
                "params": {"threadId": THREAD, "turn": {"id": "t1", "status": "completed"}},
            },
        ):
            await fwd._handle_event(
                ap,
                session_id=SESSION,
                bridge_dir=tmp_path,
                event=event,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=fwd._CodexElicitationTaskTracker(),
                expected_thread_id=THREAD,
                codex_client=codex,  # type: ignore[arg-type]
                forwarder_state=state,
            )
    kinds = [kind for kind, _ in spy.events]
    assert kinds == ["started", "usage", "finished"]
    started = spy.events[0][1]
    assert (started["session_id"], started["thread_id"], started["turn_id"]) == (
        SESSION,
        THREAD,
        "t1",
    )
    assert started["bridge_dir"] == tmp_path and started["model"] == "gpt-6.1-sol"
    assert started["codex"] is codex
    assert spy.events[1:] == [("usage", "t1"), ("finished", "t1")]


def test_the_forwarder_gate_is_inert_unless_the_runner_gate_is_provisioned(monkeypatch) -> None:
    for name in ("LLMQ_CONTROLLER_URL", "LLMQ_TOKEN_FILE", "LLMQ_AGENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIGENT_QUOTA_ROUTE", "personal-llmq")
    assert GoalContinuationGate.from_env() is None
    monkeypatch.setenv("LLMQ_CONTROLLER_URL", "http://llmq.test")
    monkeypatch.setenv("LLMQ_TOKEN_FILE", "/nonexistent")
    monkeypatch.setenv("LLMQ_AGENT_ID", ACTOR)
    assert isinstance(GoalContinuationGate.from_env(), GoalContinuationGate)
    monkeypatch.setenv("OMNIGENT_QUOTA_ROUTE", "work-vertex")
    assert GoalContinuationGate.from_env() is None


async def test_a_transient_goal_get_fault_does_not_end_the_hold(setup) -> None:
    """A Codex fault on thread/goal/get is not "the user resumed the goal".

    Ending the hold there would leave the goal paused for good with the held
    status showing, and the Turbo toggle would no longer release it.
    (Reproduced by the independent #127 review.)
    """
    llmq, config, bridge = setup
    llmq.publish(95)
    codex, server = FakeCodex(), FakeServer()

    async def on_sleep(n: int) -> None:
        if n == 1:
            codex.fail.add("thread/goal/get")  # one failed or timed-out goal read
        elif n == 2:
            codex.fail.discard("thread/goal/get")
            server.turbo = True  # the user then enables Turbo

    paused = {"threadId": THREAD, "status": "paused"}
    gate = make_gate(llmq, config, [], on_sleep)
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        # The goal starts active: wait for the hold to pause it before waiting for
        # the resume, or the initial "active" ends the wait at once.
        for _ in range(1000):
            if paused in codex.calls("thread/goal/set"):
                break
            await asyncio.sleep(0.01)
        gate.turn_finished("t1")
        for _ in range(1000):  # generous: the re-checks run against llmq's real app
            if codex.goal_status == "active":
                break
            await asyncio.sleep(0.01)
        await gate.drain()
        await gate.aclose()
    assert codex.goal_status == "active"
    assert codex.calls("thread/goal/set")[-1] == {"threadId": THREAD, "status": "active"}


async def test_the_gate_stops_activating_after_an_llmq_that_cannot_settle_unmeasured(
    setup, monkeypatch
) -> None:
    """Against an llmq without ``usage_unmeasured`` an activated, unmeasured turn
    keeps its estimate for the whole lease ("retained"). Like the runner gate,
    the goal gate then stops activating, so it strands at most one estimate."""
    llmq, config, bridge = setup
    llmq.publish(33)
    activations: list[str] = []
    outcomes: list[bool] = []

    async def activate(_client, _config, decision, *, turn_key):
        activations.append(turn_key)
        return True

    async def close(_client, _config, _decision, *, turn_key, tap, activated):
        outcomes.append(activated)
        return "retained" if activated else "released"

    monkeypatch.setattr(goal_gate_module, "activate_codex_turn", activate)
    monkeypatch.setattr(goal_gate_module, "close_codex_turn", close)
    codex, server = FakeCodex(), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        for turn in ("t1", "t2"):
            start(gate, codex, ap, bridge, turn)
            await gate.drain()
            gate.turn_finished(turn)
            await gate.drain()
        await gate.aclose()
    assert len(activations) == 1
    assert outcomes == [True, False]


async def test_the_gate_hears_a_turn_end_after_its_idle_edge_is_posted(tmp_path: Path) -> None:
    """A held continuation posts its held status once the interrupted turn ends.

    The forwarder must report the end only after it has posted that turn's own
    idle edge; otherwise the idle edge lands on top of the held status and the
    session shows idle with no hold reason or Turbo hint.
    """
    statuses: list[str] = []
    seen_at_finish: list[list[str]] = []

    class RecordingServer(FakeServer):
        def client(self) -> httpx.AsyncClient:
            async def handle(request: httpx.Request) -> httpx.Response:
                if request.method == "POST":
                    body = json.loads(request.content or b"{}")
                    data = body.get("data") if isinstance(body, dict) else None
                    if isinstance(data, dict) and isinstance(data.get("status"), str):
                        statuses.append(data["status"])
                return httpx.Response(200, json={})

            return httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://ap")

    class Spy:
        def turn_started(self, **_kwargs: Any) -> None:
            pass

        def usage_updated(self, _params: Any) -> None:
            pass

        def turn_finished(self, _turn_id: str | None) -> None:
            seen_at_finish.append(list(statuses))

    codex = FakeCodex()
    state = fwd._CodexForwarderState(
        parent_session_id=SESSION,
        codex_client=codex,  # type: ignore[arg-type]
        llmq_goal_gate=Spy(),  # type: ignore[arg-type]
    )
    server = RecordingServer()
    async with server.client() as ap:
        usage_coalescer = fwd._SessionUsageCoalescer(ap, SESSION)
        for event in (
            {
                "method": "turn/started",
                "params": {"threadId": THREAD, "turn": {"id": "t1", "status": "inProgress"}},
            },
            {
                "method": "turn/completed",
                "params": {"threadId": THREAD, "turn": {"id": "t1", "status": "interrupted"}},
            },
        ):
            await fwd._handle_event(
                ap,
                session_id=SESSION,
                bridge_dir=tmp_path,
                event=event,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=fwd._CodexElicitationTaskTracker(),
                expected_thread_id=THREAD,
                codex_client=codex,  # type: ignore[arg-type]
                forwarder_state=state,
            )
    assert seen_at_finish and "idle" in seen_at_finish[0], (seen_at_finish, statuses)


async def test_a_hold_survives_a_codex_reconnect(setup) -> None:
    """The forwarder replaces a dropped app-server client; the hold must follow it.

    A hold can last until the quota resets. After a reconnect the client captured
    at turn/started is closed, so re-checking through it would fault forever and
    the Turbo toggle would never release the goal (#128 re-review R1).
    """
    llmq, config, bridge = setup
    llmq.publish(95)
    old, server = FakeCodex(), FakeServer()
    new = FakeCodex(goal_status="paused")

    async def on_sleep(n: int) -> None:
        if n == 1:
            old.fail.update({"thread/goal/get", "thread/goal/set", "turn/interrupt"})  # closed
            gate.set_codex(new)  # the forwarder installs the reconnected client
        elif n == 2:
            server.turbo = True

    gate = make_gate(llmq, config, [], on_sleep)
    async with server.client() as ap:
        start(gate, old, ap, bridge, "t1")
        for _ in range(1000):
            if {"threadId": THREAD, "status": "paused"} in old.calls("thread/goal/set"):
                break
            await asyncio.sleep(0.01)
        gate.turn_finished("t1")
        for _ in range(1000):  # generous: the re-checks run against llmq's real app
            if new.goal_status == "active":
                break
            await asyncio.sleep(0.01)
        await gate.drain()
        await gate.aclose()
    assert new.goal_status == "active"
    assert new.calls("thread/goal/set")[-1] == {"threadId": THREAD, "status": "active"}


async def test_a_closed_gate_ignores_late_events(setup) -> None:
    """Events that arrive after aclose() (before the subscription is cancelled)
    start no work on the gate's closed llmq client."""
    llmq, config, bridge = setup
    llmq.publish(33)
    codex, server = FakeCodex(), FakeServer()
    gate = make_gate(llmq, config, [])
    await gate.aclose()
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "late")
        gate.usage_updated(usage("late", (5, 0, 1), (5, 0, 1)))
        gate.turn_finished("late")
        await gate.drain()
    assert codex.requests == [] and llmq.seen == [] and reservations(llmq) == []


async def test_an_admission_in_flight_at_aclose_starts_no_hold_after_close(setup) -> None:
    """The forwarder can stop while a refused continuation is still being admitted.

    The gate must then neither pause the goal nor start a hold loop that outlives
    it (on a closed client that loop would fault every re-check for good).
    """
    llmq, config, bridge = setup
    llmq.publish(95)
    codex, server = FakeCodex(), FakeServer()
    gate = make_gate(llmq, config, [], attribution_wait_seconds=0.3)
    mark_turn_start_pending(bridge)  # keeps the admission in flight for ~0.3 s
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t1")
        await asyncio.sleep(0)
        await gate.aclose()  # the forwarder stops mid-admission
        leaked = [t for t in gate._waits if not t.done()]
        for task in leaked:
            task.cancel()
    assert leaked == []
    assert codex.calls("thread/goal/set") == []


# --------------------------------------------------- terminal-UI turns without Stop


async def _hook_admits(llmq: Llmq, config: GateConfig, bridge: Path, turn_id: str) -> dict:
    """What the terminal-UI hook leaves behind for an admitted, activated typed turn."""
    from omnigent.runner.llmq_gate import activate_codex_turn, admit_codex_turn, turn_key

    key = turn_key(SESSION, f"tui:{turn_id}:1")
    async with llmq.client() as client:
        decision = await admit_codex_turn(
            client, config, turn_key=key, attempt=0, model="gpt-6.1-sol", turbo=False
        )
        assert decision.verified and decision.reservation_id is not None
        assert await activate_codex_turn(client, config, decision, turn_key=key) is True
    opened = {
        "codex_turn_id": turn_id,
        "turn_key": key,
        "reservation_id": decision.reservation_id,
        "activated": True,
    }
    (bridge / "llmq_gate_state.json").write_text(json.dumps({"open": opened}))
    note_admitted_turn(bridge, turn_id, clear_pending=False)
    return opened


def _counting(gate: GoalContinuationGate) -> list[str]:
    paths: list[str] = []

    async def record(request: httpx.Request) -> None:
        paths.append(request.url.path)

    gate._client().event_hooks["request"].append(record)
    return paths


async def test_a_terminal_ui_turn_that_ends_without_stop_is_settled_with_its_usage(
    setup,
) -> None:
    """Codex fires no Stop for an interrupted or failed turn (codex-cli 0.159.1), so
    the hook's reservation used to stay pending until the next prompt or the lease.
    The forwarder closes it at turn/completed with the turn's measured usage."""
    llmq, config, bridge = setup
    llmq.publish(33)
    opened = await _hook_admits(llmq, config, bridge, "t-tui")
    codex, server = FakeCodex(goal_status=None), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t-tui")
        await gate.drain()
        gate.usage_updated(usage("t-tui", (800, 100, 30), (800, 100, 30)))
        gate.turn_finished("t-tui")  # interrupted: no Stop ran
        await gate.drain()
        await gate.aclose()
    row = llmq.reservation(opened["reservation_id"])
    metadata = json.loads(row["metadata_json"])
    assert row["status"] == "settled"
    assert (metadata["native_input_tokens"], metadata["native_output_tokens"]) == (800, 30)
    assert len(llmq.seen) == 1  # the hook's admission only; the gate admitted nothing


async def test_the_hook_settling_the_same_turn_later_changes_nothing(setup) -> None:
    """The hook's next prompt settles the same turn key again (its state still says
    open): llmq answers idempotently and the measured settlement stands."""
    from omnigent import codex_llmq_hook

    llmq, config, bridge = setup
    llmq.publish(33)
    opened = await _hook_admits(llmq, config, bridge, "t-tui")
    codex, server = FakeCodex(goal_status=None), FakeServer()
    gate = make_gate(llmq, config, [])
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t-tui")
        await gate.drain()
        gate.usage_updated(usage("t-tui", (800, 100, 30), (800, 100, 30)))
        gate.turn_finished("t-tui")
        await gate.drain()
        await gate.aclose()
    async with llmq.client() as client:
        await codex_llmq_hook._close_open(client, config, dict(opened))
    row = llmq.reservation(opened["reservation_id"])
    metadata = json.loads(row["metadata_json"])
    assert row["status"] == "settled"
    assert (metadata["native_input_tokens"], metadata["native_output_tokens"]) == (800, 30)


async def test_a_turn_whose_stop_already_settled_it_is_not_closed_again(setup) -> None:
    """Codex finishes the Stop hook before turn/completed, and Stop removes the
    reservation from the hook's state: the forwarder makes no llmq call."""
    llmq, config, bridge = setup
    llmq.publish(33)
    await _hook_admits(llmq, config, bridge, "t-tui")
    (bridge / "llmq_gate_state.json").write_text(json.dumps({"session_id": SESSION}))
    codex, server = FakeCodex(goal_status=None), FakeServer()
    gate = make_gate(llmq, config, [])
    paths = _counting(gate)
    async with server.client() as ap:
        start(gate, codex, ap, bridge, "t-tui")
        await gate.drain()
        gate.turn_finished("t-tui")
        await gate.drain()
        await gate.aclose()
    assert paths == []


async def test_an_earlier_turns_open_reservation_is_left_to_the_hook(setup) -> None:
    """The hook's open record names another turn (one whose end the forwarder did
    not see): this turn's end does not touch it; the hook's next prompt settles it."""
    llmq, config, bridge = setup
    llmq.publish(33)
    opened = await _hook_admits(llmq, config, bridge, "t-earlier")
    codex, server = FakeCodex(goal_status=None), FakeServer()
    gate = make_gate(llmq, config, [])
    paths = _counting(gate)
    async with server.client() as ap:
        note_admitted_turn(bridge, "t-now", clear_pending=False)
        start(gate, codex, ap, bridge, "t-now")
        await gate.drain()
        gate.turn_finished("t-now")
        await gate.drain()
        await gate.aclose()
    assert paths == []
    assert llmq.reservation(opened["reservation_id"])["status"] == "active"


def test_the_gate_reads_the_hooks_state_file() -> None:
    from omnigent import codex_llmq_hook

    assert goal_gate_module._HOOK_STATE_FILE == codex_llmq_hook._STATE_FILE
