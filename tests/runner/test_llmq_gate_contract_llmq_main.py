"""The runner's llmq gate against llmq main's real app (contract test).

Wires omnigent.runner.llmq_gate to chatgpt-playground main's FastAPI app
(llm_quota_controller.api.create_app) through httpx.ASGITransport, so the request
body is validated by llmq's real NativeTurnAdmission model and every response the
gate parses is produced by llmq's real AdmissionDecision serializer.

Needs <chatgpt-playground>/llm-quota-controller/src on PYTHONPATH; skipped
otherwise (CI has no llmq checkout; this guards releases that compose the two).
The receipt fixture mirrors llmq's own tests
(tests/test_codex_credit_safety_margin.py::live_shaped and
tests/test_codex_reserve_quota_scope.py::fixture).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

llmq_api = pytest.importorskip("llm_quota_controller.api")

from llm_quota_controller.codex_native_contract import native_observation_batch  # noqa: E402
from llm_quota_controller.config import Settings, TokenGrant  # noqa: E402
from llm_quota_controller.engine import QuotaEngine  # noqa: E402
from llm_quota_controller.models import AdmissionDecision  # noqa: E402
from llm_quota_controller.native_turns import (  # noqa: E402
    NativeTurnAdmission,
    NativeTurnSettlement,
)

from omnigent.runner.llmq_gate import (  # noqa: E402
    GateConfig,
    GateDecision,
    TurnStreamTap,
    admit_codex_turn,
    close_codex_turn,
    effective_codex_model,
    gate_codex_turn,
    normalize_model,
    turn_key,
)

ACTOR = "fleet-test"
WEEK_MINUTES = 10080


class Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def receipt_fixture(now: int, used: float, *, credited: bool = True) -> tuple[dict, dict]:
    account = {"account": {"type": "chatgpt", "planType": "pro"}}
    ordinary: dict[str, Any] = {
        "limitId": "codex",
        "limitName": None,
        "normalModelSlug": None,
        "planType": "pro",
        "spendControlReached": False,
        "rateLimitReachedType": None,
        "credits": {"hasCredits": False, "unlimited": False},
        "individualLimit": None,
        "primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": now + 18000},
        "secondary": None,
    }
    reserve = {
        "limitId": "base_model_inference",
        "limitName": "gpt-reserve",
        "normalModelSlug": "gpt-5.6-luna",
        "planType": "pro",
        "spendControlReached": None,
        "rateLimitReachedType": None,
        "individualLimit": None,
        "credits": None,
        "primary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": now + 604800},
        "secondary": None,
    }
    if credited:
        ordinary["credits"] = {"balance": "0", "hasCredits": True, "unlimited": False}
        ordinary["primary"] = {
            "usedPercent": used,
            "windowDurationMins": WEEK_MINUTES,
            "resetsAt": now + 3 * 86400,
        }
    quota = {
        "rateLimits": deepcopy(ordinary),
        "rateLimitsByLimitId": {"codex": ordinary, "base_model_inference": reserve},
    }
    if credited:
        quota["ordinaryUsageAllowed"] = True
    return account, quota


class Llmq:
    def __init__(self, tmp_path: Path, **settings_overrides: Any) -> None:
        self.clock = Clock()
        self.receipt = tmp_path / "codex-native-receipt.json"
        base = Settings(
            db_path=tmp_path / "controller.sqlite3",
            burst_horizon_min_seconds=18_000,
            burst_horizon_max_seconds=18_000,
            stale_after_seconds=900,
            stale_deny_after_seconds=7_200,
            tokens={"agent-token": TokenGrant(role="agent", actor=ACTOR)},
        )
        self.settings = replace(base, codex_receipt_file=self.receipt, **settings_overrides)
        self.engine = QuotaEngine(self.settings, clock=self.clock)
        self.app = llmq_api.create_app(self.settings, self.engine)
        self.seen: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []

    def publish(self, used: float, *, credited: bool = True, age: float = 0.0) -> dict:
        now = int(self.clock())
        account, quota = receipt_fixture(now, used, credited=credited)
        observed_at = self.clock() - age
        self.engine.observe(native_observation_batch(account, quota, observed_at=observed_at))
        self.receipt.write_text(
            json.dumps(
                {
                    "codex_receipt": {
                        "account_response": account,
                        "quota_response": quota,
                        "observed_at": observed_at,
                    }
                }
            )
        )
        return quota

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)

        class Recording(httpx.AsyncBaseTransport):
            async def handle_async_request(inner, request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/admissions"):
                    self.seen.append(json.loads(request.content))
                response = await transport.handle_async_request(request)
                if request.url.path.endswith("/admissions"):
                    body = await response.aread()
                    self.answers.append({"status": response.status_code, "body": body.decode()})
                    return httpx.Response(
                        response.status_code, headers=response.headers, content=body
                    )
                return response

        return httpx.AsyncClient(transport=Recording())

    def reservation(self, reservation_id: str) -> sqlite3.Row:
        with self.engine.db.connect() as connection:
            return connection.execute(
                "SELECT * FROM reservations WHERE id=?", (reservation_id,)
            ).fetchone()


@pytest.fixture
def gate_config(tmp_path: Path) -> GateConfig:
    token = tmp_path / "token"
    token.write_text("agent-token\n")
    return GateConfig(
        controller_url="http://llmq.test", token_file=token, agent_id=ACTOR, recheck_seconds=30.0
    )


async def _gate(
    llmq: Llmq,
    cfg: GateConfig,
    *,
    turbo_after: int | None = None,
    model: str = "gpt-6.1-sol",
    max_sleeps: int = 10,
):
    statuses: list[tuple[GateDecision, bool]] = []
    sleeps: list[float] = []
    turbo = {"on": False}

    async def read_turbo() -> bool:
        return turbo["on"]

    async def on_status(decision: GateDecision, flag: bool) -> None:
        statuses.append((decision, flag))
        if turbo_after is not None and len(statuses) >= turbo_after:
            turbo["on"] = True

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > max_sleeps:
            raise asyncio.CancelledError  # the runner's interrupt cancels the held task

    async with llmq.client() as client:
        decision, key = await gate_codex_turn(
            client,
            cfg,
            session_id="conv_contract",
            turn_salt="salt-1",
            model=model,
            read_turbo=read_turbo,
            on_status=on_status,
            sleep=sleep,
        )
        return decision, key, statuses, sleeps, client


# ----------------------------------------------------------------- static contract


def test_request_body_validates_against_llmq_main_model() -> None:
    body = {
        "turn_key": turn_key("conv", "1"),
        "agent_id": ACTOR,
        "workstream": "agent-infra",
        "harness": "codex",
        "model": "gpt-6.1-sol",
        "attempt": 3,
        "evidence": "controller",
        "turbo": True,
    }
    parsed = NativeTurnAdmission.model_validate(body)
    assert parsed.evidence == "controller" and parsed.turbo is True and parsed.attempt == 3
    NativeTurnSettlement.model_validate(
        {
            "turn_key": turn_key("conv", "1"),
            "reservation_id": "res-00000001",
            "usage": {"input_tokens": 620, "output_tokens": 30, "cached_input_tokens": 500},
        }
    )


def test_every_response_field_the_gate_reads_exists_on_llmq_main_decision() -> None:
    read = {
        "allowed",
        "reason",
        "verified",
        "unverified_reason",
        "reservation_id",
        "credit_bearing",
        "admission_delay_seconds",
        "resets_at",
        "retry_after_seconds",
    }
    assert read <= set(AdmissionDecision.model_fields)


# ----------------------------------------------------------------- end to end


async def test_below_margin_is_verified_with_a_reservation(tmp_path, gate_config) -> None:
    llmq = Llmq(tmp_path)
    llmq.publish(33)
    decision, _key, statuses, sleeps, _ = await _gate(llmq, gate_config)
    assert (decision.allow, decision.verified, decision.credit_bearing) == (True, True, False)
    assert decision.reservation_id and statuses == [] and sleeps == []
    assert llmq.seen[0]["evidence"] == "controller" and llmq.seen[0]["attempt"] == 0


async def test_fresh_margin_holds_then_live_turbo_releases_with_fresh_attempts(
    tmp_path, gate_config
) -> None:
    llmq = Llmq(tmp_path)
    quota = llmq.publish(95)
    decision, _key, statuses, sleeps, _ = await _gate(llmq, gate_config, turbo_after=2)
    held = [s for s, _ in statuses if s.state == "blocked"]
    assert len(held) == 2
    assert held[0].reason == "credit_safety_margin"
    assert held[0].resets_at == quota["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"]
    message = held[0].message(turbo=False) or ""
    assert "credit-safety margin (90%)" in message and "resets at" in message
    assert "Enable Turbo" in message
    assert sleeps == [30.0, 30.0]
    assert [r["attempt"] for r in llmq.seen] == [0, 1, 2]
    assert [r["turbo"] for r in llmq.seen] == [False, False, True]
    assert len({r["turn_key"] for r in llmq.seen}) == 1  # same turn, fresh attempt identity
    assert decision.allow and decision.verified and decision.credit_bearing
    assert statuses[-1][0].state == "verified"  # releasing post clears the held state
    row = llmq.reservation(decision.reservation_id or "")
    assert json.loads(row["metadata_json"]).get("native_credit_bearing") is True


async def test_credit_free_exhaustion_stays_held_and_cancellation_propagates(
    tmp_path, gate_config
) -> None:
    llmq = Llmq(tmp_path)
    llmq.publish(100, credited=False)
    with pytest.raises(asyncio.CancelledError):
        await _gate(llmq, gate_config, turbo_after=1, max_sleeps=3)
    assert {r["turbo"] for r in llmq.seen[1:]} == {True}
    answers = [json.loads(a["body"]) for a in llmq.answers]
    assert {a["reason"] for a in answers} == {"quota_exhausted"}
    # no reservation was ever created for the held turn
    with llmq.engine.db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("prepare", "slug"),
    [
        (lambda q: (q.publish(95), q.receipt.unlink()), "receipt_missing"),
        (lambda q: q.publish(95, age=301), "receipt_stale"),
        (
            lambda q: (q.publish(95), q.receipt.write_text('{"unavailable": true}')),
            "receipt_unavailable",
        ),
    ],
)
async def test_undecidable_evidence_fails_open_with_slug_and_no_reservation(
    tmp_path, gate_config, prepare, slug
) -> None:
    llmq = Llmq(tmp_path)
    prepare(llmq)
    decision, _key, statuses, sleeps, _ = await _gate(llmq, gate_config)
    assert (decision.allow, decision.verified, decision.reason) == (True, False, slug)
    assert decision.reservation_id is None and sleeps == []
    assert [s.state for s, _ in statuses] == ["unverified"]


async def test_identity_mismatch_403_fails_open(tmp_path, gate_config) -> None:
    llmq = Llmq(tmp_path)
    llmq.publish(95)
    cfg = replace(gate_config, agent_id="someone-else")
    decision, *_ = await _gate(llmq, cfg)
    assert (decision.allow, decision.verified, decision.reason) == (True, False, "llmq_http_403")


async def test_the_agent_name_is_not_a_model_llmq_can_decide_on(tmp_path, gate_config) -> None:
    """omnigent's server sends ``model = agent name`` on every turn message
    (orchestration.py), and llmq cannot apply evidence to it: it answers
    ``contract_error`` and the turn runs unverified. That is why the runner never
    forwards that field and resolves the model as the harness does instead."""
    llmq = Llmq(tmp_path)
    llmq.publish(95)
    decision, _key, _statuses, _sleeps, _ = await _gate(
        llmq, gate_config, model=normalize_model("codex", gate_config.default_model)
    )
    assert llmq.seen[0]["model"] == "codex"
    assert (decision.allow, decision.verified, decision.reason) == (True, False, "contract_error")


async def test_a_session_without_a_model_override_is_held_at_the_margin(
    tmp_path, gate_config
) -> None:
    """The regression for the agent-name bug: no /model override and no spec model
    resolves to the harness default, which llmq can apply the 90% margin to."""
    llmq = Llmq(tmp_path)
    llmq.publish(95)
    model = effective_codex_model(None, None, gate_config.default_model)
    decision, _key, statuses, _sleeps, _ = await _gate(
        llmq, gate_config, model=model, turbo_after=1
    )
    assert llmq.seen[0]["model"] == model == "gpt-6.1-sol"
    assert [s.state for s, _ in statuses][:1] == ["blocked"]
    assert statuses[0][0].reason == "credit_safety_margin"
    assert (decision.allow, decision.verified, decision.credit_bearing) == (True, True, True)


def _calibrated(llmq: Llmq) -> None:
    with llmq.engine.db.immediate() as connection:
        columns = [r[1] for r in connection.execute("PRAGMA table_info(token_cost_calibration)")]
        values = {
            "provider": "openai",
            "lane": "chatgpt-codex",
            "window_name": "primary",
            "model_scope": "*",
            "ppm_per_token": 0.5,
            "sample_count": 10,
            "updated_at": llmq.clock(),
        }
        names = [c for c in columns if c in values]
        connection.execute(
            f"INSERT INTO token_cost_calibration({','.join(names)}) "
            f"VALUES ({','.join('?' for _ in names)})",
            [values[n] for n in names],
        )


def _tap(*events: dict) -> TurnStreamTap:
    tap = TurnStreamTap()
    for event in events:
        tap.feed(f"data: {json.dumps(event)}\n\n")
    return tap


CREATED = {"type": "response.created", "response": {"id": "resp_1"}}
COMPLETED = {
    "type": "response.completed",
    "response": {
        "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 40}
    },
}
COMPLETED_NO_USAGE = {"type": "response.completed", "response": {"usage": None}}


async def test_measured_usage_is_settled_as_measured_tokens(tmp_path, gate_config) -> None:
    llmq = Llmq(tmp_path)
    llmq.publish(33)
    decision, key, *_ = await _gate(llmq, gate_config)
    async with llmq.client() as c2:
        outcome = await close_codex_turn(
            c2, gate_config, decision, turn_key=key, tap=_tap(CREATED, COMPLETED)
        )
    assert outcome == "settled"
    row = llmq.reservation(decision.reservation_id or "")
    metadata = json.loads(row["metadata_json"])
    assert row["status"] == "settled"
    assert (metadata["native_input_tokens"], metadata["native_output_tokens"]) == (140, 20)
    assert metadata["native_cached_input_tokens"] == 40


async def test_unknown_usage_is_never_recorded_as_measured_zero_tokens(
    tmp_path, gate_config
) -> None:
    """The old behaviour settled {0, 0}: llmq stored it as MEASURED tokens (feeding
    _calibrate_token_cost) and, with measured charging on, charged 0 instead of the
    estimate. Now the reservation is left alone and llmq keeps its estimate."""
    llmq = Llmq(tmp_path, measured_charging_enabled=True, measured_charging_min_samples=1)
    llmq.publish(33)
    _calibrated(llmq)
    decision, key, *_ = await _gate(llmq, gate_config)
    assert decision.reservation_id
    async with llmq.client() as c2:
        outcome = await close_codex_turn(
            c2, gate_config, decision, turn_key=key, tap=_tap(CREATED, COMPLETED_NO_USAGE)
        )
    assert outcome == "kept"
    row = llmq.reservation(decision.reservation_id)
    metadata = json.loads(row["metadata_json"])
    assert row["status"] == "active" and row["estimated_ppm"] > 0
    assert row["actual_ppm"] is None  # nothing was charged as zero
    assert "native_input_tokens" not in metadata and "native_output_tokens" not in metadata


async def test_a_turn_that_never_ran_releases_the_lease_in_llmq(tmp_path, gate_config) -> None:
    llmq = Llmq(tmp_path)
    llmq.publish(33)
    decision, key, *_ = await _gate(llmq, gate_config)
    async with llmq.client() as c2:
        outcome = await close_codex_turn(
            c2, gate_config, decision, turn_key=key, tap=TurnStreamTap()
        )
    assert outcome == "cancelled"
    row = llmq.reservation(decision.reservation_id or "")
    assert row["status"] == "cancelled" and row["actual_ppm"] == 0


# ----------------------------------------------------------------- 5 s timeout, no retry


async def test_hung_controller_fails_open_after_five_seconds_without_retry(gate_config) -> None:
    connections = 0
    release = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        await release.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    cfg = replace(gate_config, controller_url=f"http://127.0.0.1:{port}")
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            started = time.monotonic()
            decision = await admit_codex_turn(
                client, cfg, turn_key="a" * 64, attempt=0, model="gpt-6.1-sol", turbo=False
            )
            elapsed = time.monotonic() - started
    finally:
        release.set()
        server.close()
        await server.wait_closed()
    assert (decision.allow, decision.verified, decision.reason) == (
        True,
        False,
        "llmq_unreachable",
    )
    assert 4.5 <= elapsed <= 7.0, elapsed
    assert connections == 1
