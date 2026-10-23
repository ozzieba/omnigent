"""The runner's pre-turn llmq gate for Codex sessions (oz 2026-10-06 / 2026-10-07).

Fail open with an indicator: every fault is an unverified admission. Hold only on
llmq's refusal on fresh evidence, re-checking with the session's current Turbo
flag, and dispatch once admitted. Settle reservations at turn end.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import llmq_gate
from omnigent.runner.llmq_gate import (
    GateConfig,
    GateDecision,
    TurnStreamTap,
    admit_codex_turn,
    cancel_codex_turn,
    close_codex_turn,
    decision_from_payload,
    effective_codex_model,
    gate_codex_turn,
    llmq_usage,
    normalize_model,
    settle_codex_turn,
    turn_key,
)


def config(tmp_path: Path, **overrides: Any) -> GateConfig:
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    base = GateConfig(
        controller_url="http://llmq.test:17645",
        token_file=token,
        agent_id="fleet-p16v",
        recheck_seconds=30.0,
        max_admission_delay_seconds=5.0,
    )
    return GateConfig(**{**base.__dict__, **overrides})


class Controller:
    """A fake llmq answering admissions from a script and recording requests."""

    def __init__(
        self,
        answers: list[Any],
        *,
        unmeasured_form: bool = True,
        activation_answer: Any = None,
    ) -> None:
        self.answers = answers
        self.unmeasured_form = unmeasured_form
        self.activation_answer = activation_answer
        self.requests: list[dict[str, Any]] = []
        self.settlements: list[dict[str, Any]] = []
        self.cancellations: list[dict[str, Any]] = []
        self.activations: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/activations"):
            self.activations.append(body)
            answer = self.activation_answer
            if answer is None:
                answer = {
                    "reservation_id": body["reservation_id"],
                    "ready": True,
                    "reason": "ready",
                }
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, int):
                return httpx.Response(answer, text="nope")
            if isinstance(answer, str):
                return httpx.Response(200, text=answer)
            return httpx.Response(200, json=answer)
        if request.url.path.endswith("/settlements"):
            if "usage_unmeasured" in body and not self.unmeasured_form:
                # An llmq from before ozzieba/chatgpt-playground#1328: extra field.
                return httpx.Response(422, json={"detail": "extra_forbidden"})
            self.settlements.append(body)
            return httpx.Response(200, json={"settlement_id": "s", "status": "settled"})
        if request.url.path.endswith("/cancellations"):
            self.cancellations.append(body)
            return httpx.Response(200, json={"settlement_id": "c", "status": "cancelled"})
        self.requests.append({**body, "_auth": request.headers.get("authorization")})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, text="nope")
        if isinstance(answer, str):
            return httpx.Response(200, text=answer)
        return httpx.Response(200, json=answer)


def client_for(controller: Controller) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(controller))


def admitted(**extra: Any) -> dict[str, Any]:
    return {
        "request_id": "r",
        "allowed": True,
        "reason": "admitted",
        "reservation_id": "res-1",
        "resolved_workstream": "agent-infra",
        "resolved_via": "x",
        "estimated_ppm": 1,
        **extra,
    }


# ----------------------------------------------------------------- config


def test_gate_is_off_unless_the_runner_env_names_controller_token_and_actor(
    tmp_path: Path,
) -> None:
    assert GateConfig.from_env({}) is None
    assert (
        GateConfig.from_env(
            {
                "OMNIGENT_QUOTA_ROUTE": "personal-llmq",
                "LLMQ_CONTROLLER_URL": "http://x",
                "LLMQ_TOKEN_FILE": "/t",
            }
        )
        is None
    )
    cfg = GateConfig.from_env(
        {
            "OMNIGENT_QUOTA_ROUTE": "personal-llmq",
            "LLMQ_CONTROLLER_URL": "http://100.87.142.18:17645/",
            "LLMQ_TOKEN_FILE": str(tmp_path / "tok"),
            "LLMQ_AGENT_ID": "fleet-p16v",
            "LLMQ_GATE_RECHECK_SECONDS": "12",
            "LLMQ_MAX_ADMISSION_DELAY_SECONDS": "bogus",
        }
    )
    assert cfg is not None
    assert cfg.controller_url == "http://100.87.142.18:17645"
    assert cfg.agent_id == "fleet-p16v"
    assert cfg.recheck_seconds == 12.0
    assert cfg.max_admission_delay_seconds == 5.0
    assert cfg.bearer() is None  # unreadable token file: the gate fails open


@pytest.mark.parametrize("route", ["work-vertex", "", "personal", "PERSONAL-LLMQ", None])
def test_gate_is_off_unless_the_launch_route_is_personal_llmq(
    tmp_path: Path, route: str | None
) -> None:
    """A work-vertex (or unclassified) launch is never metered against personal llmq."""
    env = {
        "LLMQ_CONTROLLER_URL": "http://100.87.142.18:17645",
        "LLMQ_TOKEN_FILE": str(tmp_path / "tok"),
        "LLMQ_AGENT_ID": "fleet-p16v",
    }
    if route is not None:
        env["OMNIGENT_QUOTA_ROUTE"] = route
    assert GateConfig.from_env(env) is None
    assert GateConfig.from_env({**env, "OMNIGENT_QUOTA_ROUTE": "personal-llmq"}) is not None


def test_a_token_file_that_is_not_text_fails_open(tmp_path: Path) -> None:
    token = tmp_path / "binary-token"
    token.write_bytes(b"\xff\xfe\x00bad")
    assert config(tmp_path, token_file=token).bearer() is None


def test_the_model_asked_about_is_the_one_the_harness_runs() -> None:
    # The turn's /model override wins, then the spec's pinned model, then the default.
    assert effective_codex_model("gpt-6-astra", "gpt-6.1-sol", "d") == "gpt-6-astra"
    assert effective_codex_model(None, "codex-subscription/gpt-6-astra", "d") == "gpt-6-astra"
    assert effective_codex_model("", None, "gpt-6.1-sol") == "gpt-6.1-sol"
    # The agent name that the server puts in the turn's ``model`` field is not an input,
    # so there is nothing to pass it as: a session with no override and no spec model
    # asks llmq about the default.
    assert effective_codex_model(None, None, "gpt-6.1-sol") == "gpt-6.1-sol"


def test_model_slugs_lose_catalog_prefixes() -> None:
    assert normalize_model("codex-subscription/gpt-6.1-sol", "d") == "gpt-6.1-sol"
    assert normalize_model("omnigent-openai/system.ai.gpt-5.6-sol", "d") == "gpt-5.6-sol"
    assert normalize_model("", "gpt-6.1-sol") == "gpt-6.1-sol"
    assert normalize_model(None, "d") == "d"


def test_turn_key_is_llmq_shaped_and_stable() -> None:
    key = turn_key("conv_a", "123")
    assert len(key) == 64 and int(key, 16) >= 0
    assert key == turn_key("conv_a", "123") and key != turn_key("conv_a", "124")


# ----------------------------------------------------------------- classification


def test_payload_classification() -> None:
    verified = decision_from_payload(admitted())
    assert (verified.allow, verified.verified, verified.reservation_id) == (True, True, "res-1")
    unverified = decision_from_payload(
        {
            **admitted(reservation_id=None),
            "reason": "admitted_unverified",
            "verified": False,
            "unverified_reason": "receipt_stale",
        }
    )
    assert (
        unverified.allow,
        unverified.verified,
        unverified.reason,
        unverified.reservation_id,
    ) == (
        True,
        False,
        "receipt_stale",
        None,
    )
    credited = decision_from_payload(admitted(credit_bearing=True))
    assert credited.credit_bearing is True
    refused = decision_from_payload(
        {
            **admitted(),
            "allowed": False,
            "reason": "credit_safety_margin",
            "resets_at": 1700000000.0,
        }
    )
    assert (refused.allow, refused.verified, refused.reason, refused.resets_at) == (
        False,
        True,
        "credit_safety_margin",
        1700000000.0,
    )
    paced = decision_from_payload(
        {
            **admitted(),
            "allowed": False,
            "reason": "sustainable_rate_exceeded",
            "retry_after_seconds": 7.5,
        }
    )
    assert (paced.allow, paced.retry_after_seconds) == (False, 7.5)
    undecided = decision_from_payload(
        {**admitted(), "allowed": False, "reason": "workstream_inactive"}
    )
    assert (undecided.allow, undecided.verified, undecided.reason) == (
        True,
        False,
        "workstream_inactive",
    )
    for junk in ({}, [], "x", {"allowed": "yes", "reason": "r"}, {"allowed": True}):
        bad = decision_from_payload(junk)
        assert (bad.allow, bad.verified, bad.reason) == (True, False, "llmq_unknown_shape")


def test_messages_name_the_remedy() -> None:
    held = GateDecision(allow=False, verified=True, reason="credit_safety_margin", resets_at=0.0)
    text = held.message(turbo=False)
    assert text is not None and "credit-safety margin" in text and "Enable Turbo" in text
    assert held.status_payload(turbo=False)["state"] == "blocked"
    unverified_decision = GateDecision(allow=True, verified=False, reason="llmq_unreachable")
    assert unverified_decision.message(turbo=False) == (
        "Codex quota unverified (llmq unreachable); the turn ran without llmq's check."
    )
    assert unverified_decision.message(turbo=True) == (
        "Codex quota unverified (llmq unreachable); the turn ran without llmq's check. "
        "Turbo is on, so paid credits may have been used."
    )
    assert GateDecision(allow=True, verified=True, reason="admitted").message(turbo=False) is None


# ----------------------------------------------------------------- one admission


@pytest.mark.asyncio
async def test_admission_sends_controller_evidence_turbo_and_attempt(tmp_path: Path) -> None:
    controller = Controller([admitted()])
    async with client_for(controller) as client:
        decision = await admit_codex_turn(
            client, config(tmp_path), turn_key="a" * 64, attempt=2, model="gpt-6.1-sol", turbo=True
        )
    assert decision.allow and decision.verified
    sent = controller.requests[0]
    assert sent["_auth"] == "Bearer secret-token"
    assert sent["evidence"] == "controller" and sent["turbo"] is True and sent["attempt"] == 2
    assert sent["harness"] == "codex" and sent["agent_id"] == "fleet-p16v"
    assert "codex_receipt" not in sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx.ConnectError("refused"), "llmq_unreachable"),
        (httpx.ReadTimeout("slow"), "llmq_unreachable"),
        (503, "llmq_http_503"),
        (403, "llmq_http_403"),
        ("not json", "llmq_invalid_response"),
        ({"surprise": True}, "llmq_unknown_shape"),
    ],
)
async def test_every_fault_fails_open_with_a_reason(
    tmp_path: Path, answer: Any, reason: str
) -> None:
    controller = Controller([answer])
    async with client_for(controller) as client:
        decision = await admit_codex_turn(
            client,
            config(tmp_path),
            turn_key="a" * 64,
            attempt=0,
            model="gpt-6.1-sol",
            turbo=False,
        )
    assert decision.allow is True and decision.verified is False and decision.reason == reason


@pytest.mark.asyncio
async def test_missing_token_fails_open_without_calling_llmq(tmp_path: Path) -> None:
    controller = Controller([])
    cfg = config(tmp_path, token_file=tmp_path / "absent")
    async with client_for(controller) as client:
        decision = await admit_codex_turn(
            client, cfg, turn_key="a" * 64, attempt=0, model="gpt-6.1-sol", turbo=False
        )
    assert (decision.allow, decision.verified, decision.reason) == (
        True,
        False,
        "token_unavailable",
    )
    assert controller.requests == []


# ----------------------------------------------------------------- the hold loop


@pytest.mark.asyncio
async def test_held_turn_is_released_when_turbo_is_switched_on(tmp_path: Path) -> None:
    controller = Controller(
        [
            {
                **admitted(),
                "allowed": False,
                "reason": "credit_safety_margin",
                "resets_at": 1700000000.0,
            },
            {
                **admitted(),
                "allowed": False,
                "reason": "credit_safety_margin",
                "resets_at": 1700000000.0,
            },
            admitted(credit_bearing=True),
        ]
    )
    turbo_flag = {"on": False}
    statuses: list[tuple[str, bool]] = []
    sleeps: list[float] = []

    async def read_turbo() -> bool:
        return turbo_flag["on"]

    async def on_status(decision: GateDecision, turbo: bool) -> None:
        statuses.append((decision.state, turbo))
        if len(statuses) == 2:
            turbo_flag["on"] = True  # the user flips Turbo while the turn is held

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with client_for(controller) as client:
        decision, key = await gate_codex_turn(
            client,
            config(tmp_path),
            session_id="conv_a",
            turn_salt="1",
            model="gpt-6.1-sol",
            read_turbo=read_turbo,
            on_status=on_status,
            sleep=sleep,
        )
    assert decision.allow and decision.credit_bearing and key == turn_key("conv_a", "1")
    assert [r["attempt"] for r in controller.requests] == [0, 1, 2]
    assert [r["turbo"] for r in controller.requests] == [False, False, True]
    assert statuses == [("blocked", False), ("blocked", False), ("verified", True)]
    assert sleeps == [30.0, 30.0]


@pytest.mark.asyncio
async def test_pacing_refusal_waits_for_llmq_then_proceeds(tmp_path: Path) -> None:
    controller = Controller(
        [
            {
                **admitted(),
                "allowed": False,
                "reason": "sustainable_rate_exceeded",
                "retry_after_seconds": 4.0,
            },
            admitted(admission_delay_seconds=12.0),
        ]
    )
    statuses: list[str] = []
    sleeps: list[float] = []

    async def on_status(decision: GateDecision, turbo: bool) -> None:
        statuses.append(decision.state)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def read_turbo() -> bool:
        return False

    async with client_for(controller) as client:
        decision, _ = await gate_codex_turn(
            client,
            config(tmp_path),
            session_id="conv_a",
            turn_salt="2",
            model="gpt-6.1-sol",
            read_turbo=read_turbo,
            on_status=on_status,
            sleep=sleep,
        )
    assert decision.allow and decision.verified
    # The pacing wait, then llmq's admission delay capped by the runner's budget.
    assert sleeps == [4.0, 5.0]
    assert statuses == ["blocked", "verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [(admitted(), []), (httpx.ConnectError("x"), ["unverified"])],
    ids=["verified-posts-nothing", "unverified-posts-the-indicator"],
)
async def test_verified_admission_posts_no_status_and_unverified_posts_one(
    tmp_path: Path, answer: Any, expected: list[str]
) -> None:
    controller = Controller([answer])
    statuses: list[str] = []

    async def on_status(decision: GateDecision, turbo: bool) -> None:
        statuses.append(decision.state)

    async def read_turbo() -> bool:
        return False

    async def sleep(seconds: float) -> None:
        raise AssertionError("no wait expected")

    async with client_for(controller) as client:
        decision, _ = await gate_codex_turn(
            client,
            config(tmp_path),
            session_id="conv_a",
            turn_salt="3",
            model="gpt-6.1-sol",
            read_turbo=read_turbo,
            on_status=on_status,
            sleep=sleep,
        )
    assert decision.allow
    assert statuses == expected


# ----------------------------------------------------------------- reservation lifecycle

ADMITTED = GateDecision(allow=True, verified=True, reason="admitted", reservation_id="res-9")
UNRESERVED = GateDecision(allow=True, verified=False, reason="llmq_unreachable")


def sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


COMPLETED_WITH_USAGE = {
    "type": "response.completed",
    "response": {
        "id": "resp_1",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 650,
            "cache_read_input_tokens": 500,
        },
    },
}


def test_usage_is_mapped_to_llmq_shape_and_only_when_measured() -> None:
    # Omnigent's input_tokens excludes cache reads; llmq's includes them.
    assert llmq_usage(COMPLETED_WITH_USAGE["response"]["usage"]) == {  # type: ignore[index]
        "input_tokens": 620,
        "output_tokens": 30,
        "cached_input_tokens": 500,
    }
    assert llmq_usage({"input_tokens": 7, "output_tokens": 0}) == {
        "input_tokens": 7,
        "output_tokens": 0,
    }
    # Not measured: absent, all zero (the Usage default), or malformed.
    for unmeasured in (
        None,
        {},
        "x",
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
        {"input_tokens": -1, "output_tokens": 5},
        {"input_tokens": 1.5, "output_tokens": 5},
        {"input_tokens": True, "output_tokens": 5},
    ):
        assert llmq_usage(unmeasured) is None, unmeasured


def test_tap_sees_dispatch_and_usage_across_arbitrary_chunking() -> None:
    stream = (
        sse({"type": "response.created", "response": {"id": "resp_1"}})
        + sse({"type": "response.output_text.delta", "delta": "hi"})
        + sse(COMPLETED_WITH_USAGE)
    ).encode()
    for size in (1, 7, 64, len(stream)):
        tap = TurnStreamTap()
        for start in range(0, len(stream), size):
            tap.feed(stream[start : start + size])
        assert tap.dispatched is True
        assert tap.usage == {"input_tokens": 620, "output_tokens": 30, "cached_input_tokens": 500}


def test_tap_leaves_usage_unset_when_nothing_was_measured() -> None:
    tap = TurnStreamTap()
    assert (tap.dispatched, tap.usage) == (False, None)
    tap.feed("data: not json\n\ndata: [DONE]\n\ndata: {}\n\n")
    assert (tap.dispatched, tap.usage) == (False, None)
    tap.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    tap.feed(
        sse(
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 0, "output_tokens": 0}},
            }
        )
    )
    assert (tap.dispatched, tap.usage) == (True, None)


@pytest.mark.asyncio
async def test_settlement_carries_only_the_usage_it_is_given(tmp_path: Path) -> None:
    controller = Controller([])
    async with client_for(controller) as client:
        assert await settle_codex_turn(
            client,
            config(tmp_path),
            ADMITTED,
            turn_key="b" * 64,
            usage={"input_tokens": 620, "output_tokens": 30, "cached_input_tokens": 500},
        )
        assert not await settle_codex_turn(
            client, config(tmp_path), UNRESERVED, turn_key="b" * 64, usage={"input_tokens": 1}
        )
    assert controller.settlements == [
        {
            "turn_key": "b" * 64,
            "reservation_id": "res-9",
            "usage": {"input_tokens": 620, "output_tokens": 30, "cached_input_tokens": 500},
        }
    ]


@pytest.mark.asyncio
async def test_a_turn_that_never_reached_the_harness_is_cancelled(tmp_path: Path) -> None:
    controller = Controller([])
    tap = TurnStreamTap()
    async with client_for(controller) as client:
        outcome = await close_codex_turn(
            client, config(tmp_path), ADMITTED, turn_key="c" * 64, tap=tap
        )
        none = await close_codex_turn(
            client, config(tmp_path), UNRESERVED, turn_key="c" * 64, tap=tap
        )
    assert (outcome, none) == ("cancelled", "none")
    assert controller.cancellations == [{"turn_key": "c" * 64, "reservation_id": "res-9"}]
    assert controller.settlements == []


@pytest.mark.asyncio
async def test_a_turn_that_ran_with_measured_usage_is_settled(tmp_path: Path) -> None:
    controller = Controller([])
    tap = TurnStreamTap()
    tap.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    tap.feed(sse(COMPLETED_WITH_USAGE))
    async with client_for(controller) as client:
        outcome = await close_codex_turn(
            client, config(tmp_path), ADMITTED, turn_key="d" * 64, tap=tap
        )
    assert outcome == "settled"
    assert controller.settlements[0]["usage"]["input_tokens"] == 620
    assert controller.cancellations == []


@pytest.mark.asyncio
async def test_a_turn_that_ran_with_unknown_usage_settles_at_the_estimate_never_zeros(
    tmp_path: Path,
) -> None:
    """llmq would store {0, 0} as measured tokens, and a cancel means "never ran".

    `usage_unmeasured` settles at llmq's own estimate, marked retained_estimate.
    """
    controller = Controller([])
    tap = TurnStreamTap()
    tap.feed(sse({"type": "response.created", "response": {"id": "r"}}))  # then interrupted
    async with client_for(controller) as client:
        outcome = await close_codex_turn(
            client, config(tmp_path), ADMITTED, turn_key="e" * 64, tap=tap
        )
    assert outcome == "estimated"
    assert controller.settlements == [
        {"turn_key": "e" * 64, "reservation_id": "res-9", "usage_unmeasured": True}
    ]
    assert controller.cancellations == []


@pytest.mark.asyncio
async def test_an_llmq_without_the_unmeasured_form_gets_a_cancel_instead(
    tmp_path: Path,
) -> None:
    controller = Controller([], unmeasured_form=False)
    tap = TurnStreamTap()
    tap.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    async with client_for(controller) as client:
        outcome = await close_codex_turn(
            client, config(tmp_path), ADMITTED, turn_key="e" * 64, tap=tap
        )
    assert outcome == "released"
    assert controller.settlements == []
    assert controller.cancellations == [{"turn_key": "e" * 64, "reservation_id": "res-9"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http_500", "timeout"])
async def test_an_llmq_fault_while_settling_unmeasured_never_raises_or_cancels(
    tmp_path: Path, failure: str
) -> None:
    """Fail open: a 5xx or timeout on the close is not "old llmq", so no cancel fallback."""
    cancels: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancellations"):
            cancels.append(request.url.path)
            return httpx.Response(200, json={})
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(500, json={"detail": "boom"})

    tap = TurnStreamTap()
    tap.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await close_codex_turn(
            client, config(tmp_path), ADMITTED, turn_key="e" * 64, tap=tap
        )
    assert outcome == "none"
    assert cancels == []


def _ledger_denial(retry_after: float) -> dict[str, Any]:
    return {
        **admitted(),
        "allowed": False,
        "reason": "provider_window_exhausted",
        "retry_after_seconds": retry_after,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "sleeps", "states"),
    [
        # A long ledger hint is not waited for at all: the turn runs, unverified.
        ([_ledger_denial(60.0)], [], ["unverified"]),
        # Short hints are honoured until the admission-delay budget (5 s) is spent.
        ([_ledger_denial(3.0), _ledger_denial(3.0)], [3.0], ["blocked", "unverified"]),
    ],
    ids=["long-hint-fails-open", "budget-spent-fails-open"],
)
async def test_ledger_denials_wait_at_most_the_admission_delay_then_fail_open(
    tmp_path: Path, answers: list[Any], sleeps: list[float], states: list[str]
) -> None:
    """Design #76: only the fresh-evidence margin is a refusal; throttling is a capped wait."""
    controller = Controller(answers)
    seen_states: list[str] = []
    seen_sleeps: list[float] = []

    async def on_status(decision: GateDecision, turbo: bool) -> None:
        seen_states.append(decision.state)

    async def sleep(seconds: float) -> None:
        seen_sleeps.append(seconds)

    async def read_turbo() -> bool:
        return False

    async with client_for(controller) as client:
        decision, _ = await gate_codex_turn(
            client,
            config(tmp_path),
            session_id="conv_a",
            turn_salt="7",
            model="gpt-6.1-sol",
            read_turbo=read_turbo,
            on_status=on_status,
            sleep=sleep,
        )
    assert (decision.allow, decision.verified) == (True, False)
    assert decision.reason == "llmq_provider_window_exhausted"
    assert decision.reservation_id is None
    assert seen_sleeps == sleeps and seen_states == states
    assert "retrying" not in (decision.message(turbo=False) or "")


@pytest.mark.asyncio
async def test_interrupt_between_admission_and_dispatch_cancels_the_reservation(
    tmp_path: Path,
) -> None:
    controller = Controller([admitted(admission_delay_seconds=3.0)])

    async def on_status(decision: GateDecision, turbo: bool) -> None:
        raise AssertionError("verified admissions post nothing")

    async def read_turbo() -> bool:
        return False

    async def sleep(seconds: float) -> None:
        raise asyncio.CancelledError  # the user interrupts during llmq's pacing delay

    async with client_for(controller) as client:
        with pytest.raises(asyncio.CancelledError):
            await gate_codex_turn(
                client,
                config(tmp_path),
                session_id="conv_a",
                turn_salt="4",
                model="gpt-6.1-sol",
                read_turbo=read_turbo,
                on_status=on_status,
                sleep=sleep,
            )
    assert [c["reservation_id"] for c in controller.cancellations] == ["res-1"]
    assert controller.cancellations[0]["turn_key"] == turn_key("conv_a", "4")


@pytest.mark.asyncio
async def test_cancel_without_a_reservation_posts_nothing(tmp_path: Path) -> None:
    controller = Controller([])
    async with client_for(controller) as client:
        assert not await cancel_codex_turn(client, config(tmp_path), UNRESERVED, turn_key="f" * 64)
    assert controller.cancellations == []


# ----------------------------------------------------------------- activation


@pytest.mark.asyncio
async def test_an_admitted_decision_remembers_what_it_was_admitted_for(tmp_path: Path) -> None:
    controller = Controller([admitted(credit_bearing=True)])
    async with client_for(controller) as client:
        decision = await admit_codex_turn(
            client, config(tmp_path), turn_key="a" * 64, attempt=2, model="gpt-6.1-sol", turbo=True
        )
    assert (decision.model, decision.turbo, decision.attempt) == ("gpt-6.1-sol", True, 2)
    # Bookkeeping only: two decisions for the same outcome still compare equal.
    assert decision == GateDecision(
        allow=True, verified=True, reason="admitted", reservation_id="res-1", credit_bearing=True
    )


@pytest.mark.asyncio
async def test_activation_repeats_the_admission_and_reports_whether_dispatch_was_authorised(
    tmp_path: Path,
) -> None:
    controller = Controller([admitted(credit_bearing=True)])
    cfg = config(tmp_path)
    async with client_for(controller) as client:
        decision = await admit_codex_turn(
            client, cfg, turn_key="a" * 64, attempt=2, model="gpt-6.1-sol", turbo=True
        )
        first = await llmq_gate.activate_codex_turn(client, cfg, decision, turn_key="a" * 64)
        again = await llmq_gate.activate_codex_turn(client, cfg, decision, turn_key="a" * 64)
    assert (first, again) == (True, True)
    body = controller.activations[0]
    activation_id = body.pop("activation_id")
    assert body == {
        "turn_key": "a" * 64,
        "agent_id": "fleet-p16v",
        "workstream": "agent-infra",
        "harness": "codex",
        "model": "gpt-6.1-sol",
        "attempt": 2,
        "evidence": "controller",
        "turbo": True,
        "reservation_id": "res-1",
    }
    # Stable per reservation, so a repeated activation replays in llmq.
    assert 8 <= len(activation_id) <= 200
    assert controller.activations[1]["activation_id"] == activation_id
    assert controller.settlements == [] and controller.cancellations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            {
                "reservation_id": "res-1",
                "ready": False,
                "reason": "native_turn_authority_unavailable",
            },
            False,
        ),
        (500, None),
        (403, None),
        ("not json", None),
        ({"reservation_id": "res-1"}, None),
        (httpx.ConnectError("down"), None),
        (httpx.ReadTimeout("slow"), None),
    ],
    ids=[
        "not-ready",
        "http-500",
        "http-403",
        "non-json",
        "no-ready-flag",
        "unreachable",
        "timeout",
    ],
)
async def test_activation_faults_never_raise(tmp_path: Path, answer: Any, expected: Any) -> None:
    controller = Controller([admitted()], activation_answer=answer)
    cfg = config(tmp_path)
    async with client_for(controller) as client:
        decision = await admit_codex_turn(
            client, cfg, turn_key="a" * 64, attempt=0, model="gpt-6.1-sol", turbo=False
        )
        assert (
            await llmq_gate.activate_codex_turn(client, cfg, decision, turn_key="a" * 64)
            is expected
        )
    assert len(controller.activations) == 1  # no retry


@pytest.mark.asyncio
async def test_unverified_or_unreserved_admissions_never_activate(tmp_path: Path) -> None:
    controller = Controller([])
    cfg = config(tmp_path)
    async with client_for(controller) as client:
        for decision in (
            UNRESERVED,
            GateDecision(allow=True, verified=False, reason="receipt_stale", reservation_id="r-1"),
            # A decision the gate did not get from an admission (no model): nothing to repeat.
            ADMITTED,
        ):
            assert (
                await llmq_gate.activate_codex_turn(client, cfg, decision, turn_key="a" * 64)
                is None
            )
    assert controller.activations == []


def test_the_tap_reports_dispatch_once_and_never_raises_from_its_callback() -> None:
    calls: list[str] = []
    tap = TurnStreamTap(on_dispatch=lambda: calls.append("dispatched"))
    tap.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    tap.feed(sse(COMPLETED_WITH_USAGE))
    assert calls == ["dispatched"] and tap.dispatched

    def boom() -> None:
        raise RuntimeError("callback bug")

    failing = TurnStreamTap(on_dispatch=boom)
    failing.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    assert failing.dispatched
    # A pathological line is ignored, not raised into the runner's drain loop.
    deep = TurnStreamTap()
    deep.feed('data: {"a":' + "[" * 100_000 + "\n\n")
    assert deep.dispatched is False


@pytest.mark.asyncio
async def test_turn_activation_starts_once_and_reports_only_an_authorised_dispatch() -> None:
    starts: list[int] = []

    async def start() -> bool | None:
        starts.append(1)
        return True

    activation = llmq_gate.TurnActivation(start)
    assert await activation.result() is False  # never started: nothing to report
    activation()
    activation()
    assert await activation.result() is True
    assert starts == [1]

    async def not_ready() -> bool | None:
        return False

    async def broken() -> bool | None:
        raise RuntimeError("bug")

    for fn in (not_ready, broken):
        other = llmq_gate.TurnActivation(fn)
        other()
        assert await other.result() is False


@pytest.mark.asyncio
async def test_an_activated_unmeasured_turn_is_never_cancelled_on_an_llmq_without_the_form(
    tmp_path: Path,
) -> None:
    """llmq answers 409 to a cancel once a turn was activated; the turn did run."""
    controller = Controller([], unmeasured_form=False)
    tap = TurnStreamTap()
    tap.feed(sse({"type": "response.created", "response": {"id": "r"}}))
    async with client_for(controller) as client:
        outcome = await close_codex_turn(
            client, config(tmp_path), ADMITTED, turn_key="e" * 64, tap=tap, activated=True
        )
    assert outcome == "retained"
    assert controller.settlements == [] and controller.cancellations == []


# ----------------------------------------------------------------- turn attribution


def test_turn_attribution_records_admitted_turns_and_in_flight_starts(tmp_path: Path) -> None:
    record = llmq_gate.turn_admission_record
    assert record(tmp_path, "turn_a") == "unknown"
    llmq_gate.mark_turn_start_pending(tmp_path)
    assert record(tmp_path, "turn_a") == "pending"  # may be the start in flight
    llmq_gate.note_admitted_turn(tmp_path, "turn_a")
    assert record(tmp_path, "turn_a") == "admitted"
    assert record(tmp_path, "turn_b") == "unknown"  # recording clears the pending marker
    llmq_gate.mark_turn_start_pending(tmp_path)
    llmq_gate.clear_turn_start_pending(tmp_path)
    assert record(tmp_path, "turn_b") == "unknown"


def test_a_stale_pending_start_is_not_waited_for(tmp_path: Path) -> None:
    llmq_gate.mark_turn_start_pending(tmp_path)
    (tmp_path / llmq_gate.TURN_START_PENDING_FILE).write_text(
        str(llmq_gate.time.time() - llmq_gate.TURN_START_PENDING_MAX_AGE_S - 1)
    )
    assert llmq_gate.turn_admission_record(tmp_path, "turn_a") == "unknown"


def test_turn_attribution_stays_bounded_and_never_raises(tmp_path: Path) -> None:
    for n in range(3000):
        llmq_gate.note_admitted_turn(tmp_path, f"019a-turn-{n:06d}")
    path = tmp_path / llmq_gate.ADMITTED_TURNS_FILE
    assert path.stat().st_size <= 64 * 1024 + 64
    assert llmq_gate.turn_admission_record(tmp_path, "019a-turn-002999") == "admitted"
    # An unwritable bridge dir is logged, not raised into the executor.
    missing = tmp_path / "gone"
    llmq_gate.mark_turn_start_pending(missing)
    llmq_gate.note_admitted_turn(missing, "turn_x")
    assert llmq_gate.turn_admission_record(missing, "turn_x") == "unknown"
