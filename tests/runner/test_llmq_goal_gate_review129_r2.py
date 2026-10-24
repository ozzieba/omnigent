"""review129 round 2: the forwarder closing terminal-UI reservations (0739b7fc).

The REAL hook (:func:`omnigent.codex_llmq_hook.main`, driven as Codex runs it), the
REAL :class:`GoalContinuationGate` (driven through the forwarder's own event handler
where the wiring matters) and llmq's REAL app (ASGI), all on one bridge dir. Needs
llmq on PYTHONPATH.

Real-Codex 0.159.1 evidence for the event order these tests assume (review129
``exp/r2_exp.py``, ``r2-int-late.txt`` / ``r2-fail.txt``): an interrupted or failed
typed turn emits ``turn/completed`` (status interrupted / failed) with no ``Stop``,
after every ``UserPromptSubmit`` hook completed; the forwarder's close follows
about 30 ms later.
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
from omnigent.runner.llmq_gate import GateConfig
from omnigent.runner.llmq_goal_gate import GoalContinuationGate
from tests.runner.test_codex_llmq_hook_contract_llmq_main import SESSION, Tui
from tests.runner.test_llmq_gate_contract_llmq_main import ACTOR, UNMEASURED_FORM, Llmq

THREAD = "thread_1"
#: Captured before the ``tui`` fixture routes every new client to llmq's app.
_RealAsyncClient = httpx.AsyncClient


@pytest.fixture
def llmq(tmp_path: Path) -> Llmq:
    return Llmq(tmp_path)


@pytest.fixture
def tui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llmq: Llmq) -> Tui:
    return Tui(tmp_path, monkeypatch, llmq)


class _NoGoalCodex:
    """The app-server as the gate sees it: no goal on the thread."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "thread/goal/get":
            return {"id": 1, "result": {"goal": None}}
        return {"id": 2, "result": {}}


def _gate(tmp_path: Path, http: httpx.AsyncClient | None = None) -> GoalContinuationGate:
    config = GateConfig(
        controller_url="http://llmq.test", token_file=tmp_path / "token", agent_id=ACTOR
    )
    return GoalContinuationGate(config, http=http, attribution_wait_seconds=0.0)


def _usage(turn_id: str, inp: int, cached: int, out: int) -> dict[str, Any]:
    breakdown = {
        "inputTokens": inp,
        "cachedInputTokens": cached,
        "outputTokens": out,
        "totalTokens": inp + out,
    }
    return {
        "threadId": THREAD,
        "turnId": turn_id,
        "tokenUsage": {"last": breakdown, "total": dict(breakdown)},
    }


async def _event(gate: GoalContinuationGate, bridge: Path, event: dict[str, Any]) -> None:
    """One app-server notification through the forwarder's real handler."""
    codex = _NoGoalCodex()
    state = fwd._CodexForwarderState(
        parent_session_id=SESSION,
        codex_client=codex,  # type: ignore[arg-type]
        llmq_goal_gate=gate,
    )
    state.model = "gpt-6.1-sol"

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with _RealAsyncClient(transport=httpx.MockTransport(handle), base_url="http://ap") as ap:
        await fwd._handle_event(
            ap,
            session_id=SESSION,
            bridge_dir=bridge,
            event=event,
            usage_coalescer=fwd._SessionUsageCoalescer(ap, SESSION),
            elicitation_tracker=fwd._CodexElicitationTaskTracker(),
            expected_thread_id=THREAD,
            codex_client=codex,  # type: ignore[arg-type]
            forwarder_state=state,
        )


def _started(turn_id: str) -> dict[str, Any]:
    return {
        "method": "turn/started",
        "params": {"threadId": THREAD, "turn": {"id": turn_id, "status": "inProgress"}},
    }


def _ended(turn_id: str, status: str = "interrupted") -> dict[str, Any]:
    return {
        "method": "turn/completed",
        "params": {"threadId": THREAD, "turn": {"id": turn_id, "status": status}},
    }


def _metadata(llmq: Llmq, reservation_id: str) -> tuple[str, dict[str, Any]]:
    row = llmq.reservation(reservation_id)
    return row["status"], json.loads(row["metadata_json"])


# ---------------------------------------------------------------- R2-1: missed turn/started


async def test_a_turn_end_whose_start_the_forwarder_missed_still_closes_the_hook_turn(
    tmp_path: Path, tui: Tui, llmq: Llmq
) -> None:
    """R2-1 (Low). The forwarder can miss ``turn/started`` while it reconnects (or a
    restarted runner's new gate never saw it), but it always knows its bridge dir.
    0739b7fc keys the hook-state lookup on the bridge recorded at ``turn_started``,
    so such a turn's end leaves the hook's reservation pending until the session's
    next prompt or llmq's 24 h lease: exactly what 0739b7fc set out to close.
    """
    llmq.publish(33)
    assert await asyncio.to_thread(tui.run, "prompt-submit", "turn-1") == {}
    (opened,) = tui.reservations()
    gate = _gate(tmp_path)
    await _event(gate, tui.bridge, _ended("turn-1"))  # no turn/started seen
    await gate.drain()
    await gate.aclose()
    assert llmq.reservation(opened["id"])["status"] == "settled"


# ---------------------------------------------------------------- characterisation (GREEN)


async def test_an_interrupted_typed_turn_settles_at_turn_end_and_the_next_prompt_is_a_no_op(
    tmp_path: Path, tui: Tui, llmq: Llmq
) -> None:
    """The happy path end to end: hook admits + activates, Codex interrupts (no Stop),
    the forwarder settles with measured usage, the next prompt's re-settle of the same
    turn key is refused as a different body (409) and changes nothing."""
    llmq.publish(33)
    assert await asyncio.to_thread(tui.run, "prompt-submit", "turn-1") == {}
    (opened,) = tui.reservations()
    gate = _gate(tmp_path)
    await _event(gate, tui.bridge, _started("turn-1"))
    await gate.drain()
    gate.usage_updated(_usage("turn-1", 800, 100, 30))
    await _event(gate, tui.bridge, _ended("turn-1"))
    await gate.drain()
    status, metadata = _metadata(llmq, opened["id"])
    assert status == "settled"
    assert (metadata["native_input_tokens"], metadata["native_output_tokens"]) == (800, 30)
    assert await asyncio.to_thread(tui.run, "prompt-submit", "turn-2") == {}
    await gate.aclose()
    status, metadata = _metadata(llmq, opened["id"])
    assert status == "settled" and metadata["native_output_tokens"] == 30
    assert tui.calls("cancellations") == []
    # The forwarder's measured settlement (200), then the hook's re-settle (409).
    closes = [s for s, b in tui.calls("settlements") if b["reservation_id"] == opened["id"]]
    assert closes == [200, 409]


async def test_when_the_hook_settles_first_the_measured_usage_is_lost_but_nothing_breaks(
    tmp_path: Path, tui: Tui, llmq: Llmq
) -> None:
    """Order reversed (forwarder backlogged behind slow Omnigent posts while the next
    prompt's hook runs): the estimate wins, the forwarder's measured close gets 409.
    Never a cancel, never zero, settled once."""
    llmq.publish(33)
    assert await asyncio.to_thread(tui.run, "prompt-submit", "turn-1") == {}
    (opened,) = tui.reservations()
    gate = _gate(tmp_path)
    await _event(gate, tui.bridge, _started("turn-1"))
    await gate.drain()
    gate.usage_updated(_usage("turn-1", 800, 100, 30))
    assert await asyncio.to_thread(tui.run, "prompt-submit", "turn-2") == {}
    await _event(gate, tui.bridge, _ended("turn-1"))
    await gate.drain()
    await gate.aclose()
    status, metadata = _metadata(llmq, opened["id"])
    assert status == "settled"
    if UNMEASURED_FORM:
        assert metadata.get("native_usage_basis") == "retained_estimate"
    assert tui.calls("cancellations") == []


async def test_concurrent_forwarder_close_and_next_prompt_settle_once(
    tmp_path: Path, tui: Tui, llmq: Llmq
) -> None:
    """Both closers at the same moment (hook in a thread, forwarder on the loop)."""
    llmq.publish(33)
    assert await asyncio.to_thread(tui.run, "prompt-submit", "turn-1") == {}
    (opened,) = tui.reservations()
    gate = _gate(tmp_path)
    await _event(gate, tui.bridge, _started("turn-1"))
    await gate.drain()
    gate.usage_updated(_usage("turn-1", 800, 100, 30))
    hook = asyncio.to_thread(tui.run, "prompt-submit", "turn-2")
    forwarder = _event(gate, tui.bridge, _ended("turn-1"))
    out, _ = await asyncio.gather(hook, forwarder)
    await gate.drain()
    await gate.aclose()
    assert out == {}
    assert llmq.reservation(opened["id"])["status"] == "settled"
    assert tui.calls("cancellations") == []
    closes = [s for s, b in tui.calls("settlements") if b["reservation_id"] == opened["id"]]
    assert sorted(closes) in ([200, 409], [200, 200])  # 200+200: same body, a replay


async def test_a_prompt_blocked_at_the_margin_is_never_touched_by_the_forwarder(
    tmp_path: Path, tui: Tui, llmq: Llmq
) -> None:
    """A hook-blocked prompt has no reservation; Codex still emits turn/started and
    turn/completed for it. The forwarder makes no llmq call."""
    llmq.publish(95)
    out = await asyncio.to_thread(tui.run, "prompt-submit", "turn-1")
    assert out.get("decision") == "block"
    assert tui.reservations() == []
    before = len(tui.llmq_calls)
    gate = _gate(tmp_path)
    await _event(gate, tui.bridge, _started("turn-1"))
    await gate.drain()
    await _event(gate, tui.bridge, _ended("turn-1", "completed"))
    await gate.drain()
    await gate.aclose()
    assert tui.llmq_calls[before:] == []


async def test_an_old_llmq_never_gets_a_cancel_for_an_activated_hook_turn(
    tmp_path: Path, tui: Tui, llmq: Llmq
) -> None:
    """llmq without #1328 (``usage_unmeasured`` -> 422): an activated turn that ended
    without Stop and without measured usage is retained, never cancelled."""
    llmq.publish(33)
    assert await asyncio.to_thread(tui.run, "prompt-submit", "turn-1") == {}
    (opened,) = tui.reservations()
    assert (
        json.loads((tui.bridge / "llmq_gate_state.json").read_text())["open"]["activated"] is True
    )
    asgi = httpx.ASGITransport(app=llmq.app)
    paths: list[str] = []

    class OldLlmq(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path.rsplit("/", 1)[-1])
            body = json.loads(request.content or b"{}")
            if request.url.path.endswith("/settlements") and "usage_unmeasured" in body:
                return httpx.Response(422, json={"detail": "extra field"})
            return await asgi.handle_async_request(request)

    gate = _gate(tmp_path, http=_RealAsyncClient(transport=OldLlmq()))
    await _event(gate, tui.bridge, _started("turn-1"))
    await gate.drain()
    await _event(gate, tui.bridge, _ended("turn-1"))
    await gate.drain()
    await gate.aclose()
    assert paths == ["settlements"]
    assert llmq.reservation(opened["id"])["status"] == "active"  # retained for the lease


async def test_a_failed_turn_is_closed_with_its_id_from_either_terminal_shape(
    tmp_path: Path, tui: Tui, llmq: Llmq
) -> None:
    """``turn/failed`` with a top-level ``turnId`` (no nested turn object)."""
    llmq.publish(33)
    assert await asyncio.to_thread(tui.run, "prompt-submit", "turn-1") == {}
    (opened,) = tui.reservations()
    gate = _gate(tmp_path)
    await _event(gate, tui.bridge, _started("turn-1"))
    await gate.drain()
    await _event(
        gate,
        tui.bridge,
        {"method": "turn/failed", "params": {"threadId": THREAD, "turnId": "turn-1"}},
    )
    await gate.drain()
    await gate.aclose()
    assert llmq.reservation(opened["id"])["status"] == "settled"
