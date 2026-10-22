"""The gate as wired into the runner's background turn (needs llmq on PYTHONPATH).

Drives create_runner_app through POST /v1/sessions/{conv}/events (the production
background path) with a fake harness, a recording fake Omnigent server, and the
REAL gate talking to llmq main's real app through an ASGI transport. Proves:

- a held Codex turn does not reach the harness; the ORIGINAL message reaches it
  exactly once after a live Turbo toggle releases it;
- each re-check reads Turbo fresh from GET /v1/sessions/{id} and bumps attempt;
- quota_gate status posts (blocked, blocked, verified) reach the server;
- an interrupt while held cancels the turn without dispatch;
- a non-Codex harness never consults the gate;
- the model asked about is the harness's (override, spec, default), never the agent name;
- reservations end on every exit: settled with measured usage, cancelled when the harness
  was never reached (spawn failure, rejected delivery), and left alone (never settled with
  placeholder zeros) when the turn ran with unknown usage, including an interrupt;
- the gate is inert for a work-vertex launch; an unexpected gate fault still shows the
  indicator and the turn runs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("llm_quota_controller.api")

import omnigent.runner.app as runner_app_mod
from omnigent.runner import (
    create_runner_app,
    llmq_gate,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.test_llmq_gate_contract_llmq_main import ACTOR, Llmq
from tests.runner.test_runner_dispatch import (
    _FakeHarnessStream,
    _runner_test_client,
)

DONE_CHUNKS = [
    'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
    (
        'event: response.completed\ndata: {"type":"response.completed",'
        '"response":{"id":"resp_1","status":"completed","output":[]}}\n\n'
    ),
]
DONE_WITH_USAGE_CHUNKS = [
    DONE_CHUNKS[0],
    (
        'event: response.completed\ndata: {"type":"response.completed","response":'
        '{"id":"resp_1","status":"completed","output":[],"usage":'
        '{"input_tokens":100,"output_tokens":20,"cache_read_input_tokens":40}}}\n\n'
    ),
]


class RecordingHarness:
    def __init__(self, status_code: int = 200, chunks: list[str] | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.status_code = status_code
        self.chunks = DONE_CHUNKS if chunks is None else chunks

    def stream(self, method: str, url: str, *, json: dict, timeout: float | None):
        self.bodies.append(json)
        return _FakeHarnessStream(self.chunks, status_code=self.status_code)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={})


class FakePM:
    def __init__(self, harness: RecordingHarness) -> None:
        self.harness = harness

    async def get_client(self, conversation_id: str, harness_name: str, *, env=None):
        return self.harness

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        pass

    def clear_in_flight(self, conversation_id: str) -> None:
        pass

    async def release(self, conversation_id: str, **kwargs: object) -> None:
        pass


class FakeServer:
    """Answers GET /v1/sessions/{id} with the live Turbo flag; records status posts."""

    def __init__(self) -> None:
        self.turbo = False
        self.turbo_reads = 0
        self.gate_posts: list[dict[str, Any]] = []

    class _Resp:
        def __init__(self, body: Any, status: int = 200) -> None:
            self._body = body
            self.status_code = status

        def json(self) -> Any:
            return self._body

        def raise_for_status(self) -> None:
            pass

    async def get(self, url: str, **kwargs: Any):
        if url.count("/") == 3 and url.startswith("/v1/sessions/"):
            self.turbo_reads += 1
            return self._Resp({"turbo": self.turbo})
        return self._Resp({})

    async def post(self, url: str, **kwargs: Any):
        body = kwargs.get("json") or {}
        if isinstance(body, dict) and body.get("type") == "external_session_status":
            data = body.get("data") or {}
            if "quota_gate" in data:
                self.gate_posts.append(data)
        return self._Resp({})

    async def patch(self, url: str, **kwargs: Any):
        return self._Resp({})

    async def put(self, url: str, **kwargs: Any):
        return self._Resp({})

    async def delete(self, url: str, **kwargs: Any):
        return self._Resp({})


def _spec(harness: str, model: str | None = None):
    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return AgentSpec(
            spec_version=1,
            name="codex-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": harness}, model=model),
        )

    return resolver


@pytest.fixture
def wired(monkeypatch, tmp_path: Path):
    token = tmp_path / "token"
    token.write_text("agent-token\n")
    monkeypatch.setenv("OMNIGENT_QUOTA_ROUTE", "personal-llmq")
    monkeypatch.setenv("LLMQ_CONTROLLER_URL", "http://llmq.test")
    monkeypatch.setenv("LLMQ_TOKEN_FILE", str(token))
    monkeypatch.setenv("LLMQ_AGENT_ID", ACTOR)
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.missing_harness_cli", lambda harness: None
    )
    monkeypatch.setattr(runner_app_mod, "_build_spawn_env_from_spec", lambda *a, **k: {})
    llmq = Llmq(tmp_path)
    server = FakeServer()
    state: dict[str, Any] = {"gate_calls": 0, "sleeps": 0, "closes": [], "harness": None}
    real_gate = llmq_gate.gate_codex_turn
    real_close = llmq_gate.close_codex_turn

    async def gate(client, config, **kwargs):
        state["gate_calls"] += 1
        real_sleep = kwargs["sleep"]

        async def fast_sleep(seconds: float) -> None:
            state["sleeps"] += 1
            # while held, the harness must not have been reached yet
            assert state["harness"] is None or state["harness"].bodies == []
            hook = state.get("on_sleep")
            if hook is not None:
                await hook(state["sleeps"])
            await real_sleep(0)

        kwargs["sleep"] = fast_sleep
        async with llmq.client() as c:
            return await real_gate(c, config, **kwargs)

    async def close(client, config, decision, **kwargs):
        async with llmq.client() as c:
            outcome = await real_close(c, config, decision, **kwargs)
        state["closes"].append((decision.reservation_id, outcome))
        return outcome

    monkeypatch.setattr(runner_app_mod, "gate_codex_turn", gate)
    monkeypatch.setattr(runner_app_mod, "close_codex_turn", close)
    return llmq, server, state


async def _post(app, conv: str, text: str, *, model_override: str | None = "gpt-6.1-sol") -> None:
    body: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "agent_id": "ag_codex",
        # The server fills ``model`` with the AGENT NAME (a display tag).
        "model": "codex-agent",
        "content": [{"type": "input_text", "text": text}],
    }
    if model_override is not None:
        body["model_override"] = model_override
    async with _runner_test_client(app) as http:
        response = await http.post(f"/v1/sessions/{conv}/events", json=body)
        assert response.status_code == 202, response.text


async def _wait_turn(conv: str, timeout: float = 15.0) -> asyncio.Task | None:
    task = next((t for t in asyncio.all_tasks() if t.get_name() == f"turn-{conv}"), None)
    if task is not None:
        await asyncio.wait({task}, timeout=timeout)
    return task


async def _settle_bg() -> None:
    for _ in range(50):
        pending = [t for t in asyncio.all_tasks() if t.get_name().startswith("llmq-close-")]
        if not pending:
            return
        await asyncio.sleep(0.02)


async def test_held_turn_dispatches_original_message_once_after_turbo(wired) -> None:
    llmq, server, state = wired
    llmq.publish(95)
    harness = RecordingHarness()
    state["harness"] = harness

    async def on_sleep(n: int) -> None:
        if n == 2:
            server.turbo = True  # user flips Turbo while the turn is held

    state["on_sleep"] = on_sleep
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, FakePM(harness)),
        spec_resolver=_spec("codex"),
        server_client=server,  # type: ignore[arg-type]
    )
    await _post(app, "conv_held", "original prompt text")
    await _wait_turn("conv_held")
    await _settle_bg()
    assert len(harness.bodies) == 1
    sent = json.dumps(harness.bodies[0]["content"])
    assert "original prompt text" in sent
    assert [p["quota_gate"]["state"] for p in server.gate_posts] == [
        "blocked",
        "blocked",
        "verified",
    ]
    assert server.gate_posts[0]["blocked_on"].startswith("Codex subscription quota is at")
    assert "blocked_on" not in server.gate_posts[-1]
    assert [r["attempt"] for r in llmq.seen] == [0, 1, 2]
    assert [r["turbo"] for r in llmq.seen] == [False, False, True]
    assert server.turbo_reads >= 3
    # The harness reported no usage: the reservation is left alone, never settled with zeros.
    assert len(state["closes"]) == 1 and state["closes"][0][1] == "kept"
    row = llmq.reservation(state["closes"][0][0])
    assert row["status"] == "active"
    assert "native_input_tokens" not in json.loads(row["metadata_json"])


async def test_interrupt_while_held_never_dispatches(wired) -> None:
    llmq, server, state = wired
    llmq.publish(95)
    harness = RecordingHarness()
    state["harness"] = harness
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, FakePM(harness)),
        spec_resolver=_spec("codex"),
        server_client=server,  # type: ignore[arg-type]
    )
    cancelled = asyncio.Event()

    async def on_sleep(n: int) -> None:
        if n == 3:
            task = next(t for t in asyncio.all_tasks() if t.get_name() == "turn-conv_int")
            task.cancel()
            cancelled.set()

    state["on_sleep"] = on_sleep
    await _post(app, "conv_int", "should never run")
    task = next((t for t in asyncio.all_tasks() if t.get_name() == "turn-conv_int"), None)
    if task is not None:
        await asyncio.wait({task}, timeout=15.0)
    assert cancelled.is_set()
    assert harness.bodies == []
    with llmq.engine.db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


async def test_claude_harness_is_not_gated(wired) -> None:
    llmq, server, state = wired
    llmq.publish(95)
    harness = RecordingHarness()
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, FakePM(harness)),
        spec_resolver=_spec("claude-sdk"),
        server_client=server,  # type: ignore[arg-type]
    )
    await _post(app, "conv_claude", "hi")
    await _wait_turn("conv_claude")
    assert state["gate_calls"] == 0 and llmq.seen == [] and server.gate_posts == []
    assert len(harness.bodies) == 1


class BlockingHarness(RecordingHarness):
    """A wrapped-codex turn that is still streaming when the user interrupts."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    def stream(self, method: str, url: str, *, json: dict, timeout: float | None):
        self.bodies.append(json)
        outer = self

        class _Blocking(_FakeHarnessStream):
            async def aiter_text(self):
                yield DONE_CHUNKS[0]
                outer.started.set()
                await asyncio.Event().wait()

        return _Blocking(DONE_CHUNKS)


def _runner(harness, wired_state, spec, *, server, pm=None):
    wired_state["harness"] = harness
    return create_runner_app(
        process_manager=cast(HarnessProcessManager, pm or FakePM(harness)),
        spec_resolver=spec,
        server_client=server,  # type: ignore[arg-type]
    )


def _reservations(llmq) -> list[sqlite3.Row]:
    with llmq.engine.db.connect() as connection:
        return connection.execute("SELECT * FROM reservations").fetchall()


async def test_interrupt_after_dispatch_keeps_the_estimate_and_never_settles_zeros(wired) -> None:
    llmq, server, state = wired
    llmq.publish(33)
    harness = BlockingHarness()
    app = _runner(harness, state, _spec("codex"), server=server)
    await _post(app, "conv_int2", "long running turn")
    await asyncio.wait_for(harness.started.wait(), timeout=10)
    task = next(t for t in asyncio.all_tasks() if t.get_name() == "turn-conv_int2")
    task.cancel()  # what _cancel_inprocess_turn does on interrupt/stop
    await asyncio.wait({task}, timeout=10)
    await _settle_bg()
    rows = _reservations(llmq)
    assert len(rows) == 1 and len(harness.bodies) == 1
    # The turn ran (response.created was seen) with usage unknown: the close step ran
    # on the interrupt, and left llmq's estimate in place rather than settling zeros.
    assert state["closes"] == [(rows[0]["id"], "kept")]
    assert rows[0]["status"] == "active" and rows[0]["actual_ppm"] is None
    assert "native_input_tokens" not in json.loads(rows[0]["metadata_json"])


async def test_spawn_failure_after_admission_cancels_the_reservation(wired) -> None:
    llmq, server, state = wired
    llmq.publish(33)

    class FailingPM(FakePM):
        async def get_client(self, conversation_id: str, harness_name: str, *, env=None):
            raise RuntimeError("harness failed to start")

    harness = RecordingHarness()
    app = _runner(harness, state, _spec("codex"), server=server, pm=FailingPM(harness))
    await _post(app, "conv_spawn", "never dispatched")
    await _wait_turn("conv_spawn")
    await _settle_bg()
    rows = _reservations(llmq)
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"  # admitted, never dispatched, lease released
    assert state["closes"] == [(rows[0]["id"], "cancelled")]


async def test_rejected_delivery_cancels_the_reservation(wired) -> None:
    llmq, server, state = wired
    llmq.publish(33)
    harness = RecordingHarness(status_code=500)
    app = _runner(harness, state, _spec("codex"), server=server)
    await _post(app, "conv_reject", "harness says no")
    await _wait_turn("conv_reject")
    await _settle_bg()
    rows = _reservations(llmq)
    assert len(rows) == 1 and rows[0]["status"] == "cancelled"


async def test_measured_usage_from_the_harness_is_settled(wired) -> None:
    llmq, server, state = wired
    llmq.publish(33)
    harness = RecordingHarness(chunks=DONE_WITH_USAGE_CHUNKS)
    app = _runner(harness, state, _spec("codex"), server=server)
    await _post(app, "conv_usage", "count my tokens")
    await _wait_turn("conv_usage")
    await _settle_bg()
    rows = _reservations(llmq)
    assert len(rows) == 1 and rows[0]["status"] == "settled"
    assert state["closes"] == [(rows[0]["id"], "settled")]
    metadata = json.loads(rows[0]["metadata_json"])
    assert (metadata["native_input_tokens"], metadata["native_output_tokens"]) == (140, 20)


@pytest.mark.parametrize(
    ("spec_model", "default_model"),
    [(None, None), ("codex-subscription/gpt-6.1-sol", "gpt-6-astra")],
    ids=["harness-default", "spec-model-beats-the-default"],
)
async def test_a_session_without_a_model_override_asks_llmq_about_the_harness_model(
    wired, monkeypatch, spec_model: str | None, default_model: str | None
) -> None:
    """Regression: the turn message's ``model`` is the agent NAME ("codex-agent"). It was
    sent to llmq as the model, which llmq rejects, so a session at 95% was never held."""
    llmq, server, state = wired
    llmq.publish(95)
    harness = RecordingHarness()
    if default_model is not None:
        monkeypatch.setenv("LLMQ_MODEL", default_model)

    async def on_sleep(n: int) -> None:
        if n == 1:
            server.turbo = True

    state["on_sleep"] = on_sleep
    app = _runner(harness, state, _spec("codex", model=spec_model), server=server)
    await _post(app, "conv_nomodel", "no override here", model_override=None)
    await _wait_turn("conv_nomodel")
    await _settle_bg()
    assert {r["model"] for r in llmq.seen} == {"gpt-6.1-sol"}
    assert llmq.seen[0]["model"] != "codex-agent"
    # The 90% margin was enforced (held once), not skipped as an unsupported model.
    assert [p["quota_gate"]["state"] for p in server.gate_posts] == ["blocked", "verified"]
    assert len(harness.bodies) == 1


async def test_a_work_vertex_launch_is_never_gated(wired, monkeypatch) -> None:
    llmq, server, state = wired
    llmq.publish(95)
    monkeypatch.setenv("OMNIGENT_QUOTA_ROUTE", "work-vertex")
    harness = RecordingHarness()
    app = _runner(harness, state, _spec("codex"), server=server)
    await _post(app, "conv_work", "work-route codex turn")
    await _wait_turn("conv_work")
    assert state["gate_calls"] == 0 and llmq.seen == [] and server.gate_posts == []
    assert len(harness.bodies) == 1


async def test_an_unexpected_gate_fault_runs_the_turn_with_the_indicator(
    wired, monkeypatch
) -> None:
    llmq, server, state = wired
    llmq.publish(33)
    harness = RecordingHarness()
    app = _runner(harness, state, _spec("codex"), server=server)

    async def boom(client, config, **kwargs):
        raise ValueError("gate bug")

    monkeypatch.setattr(runner_app_mod, "gate_codex_turn", boom)
    await _post(app, "conv_fault", "still runs")
    await _wait_turn("conv_fault")
    assert len(harness.bodies) == 1
    assert [(p["quota_gate"]["state"], p["quota_gate"]["reason"]) for p in server.gate_posts] == [
        ("unverified", "gate_error")
    ]
