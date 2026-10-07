"""Pre-turn llmq admission for Codex sessions: fail open, with an indicator.

oz, 2026-10-06: "gate via llmq, but with a per session 'turbo' button on omnigent
right next to the model selector". oz, 2026-10-07: "this is not a security boundary;
in fact llmq should fail open but with an indicator".

The runner asks llmq's native-turn admission before it hands a Codex turn to the
harness. llmq holds the Codex receipt (``evidence="controller"``), so no host needs
a local receipt. Every fault (unreachable, timeout, HTTP error, unknown shape) and
every "cannot decide" answer is an ALLOW with ``verified=False`` and a reason the
session shows. The only refusals are llmq's own decisions on fresh evidence: at or
past the credit-safety margin with Turbo off (``credit_safety_margin``), or a
credit-free account at exhaustion (``quota_exhausted``); the turn is then held,
re-checked periodically with the session's current Turbo flag, and dispatched
unchanged once admitted. Rate shaping below the margin keeps llmq's availability
semantics: the turn waits for ``retry_after_seconds``.

The gate is active only where the runner's environment names the controller
(``LLMQ_CONTROLLER_URL``), its credential (``LLMQ_TOKEN_FILE``) and this host's
llmq actor (``LLMQ_AGENT_ID``); otherwise :func:`GateConfig.from_env` returns
``None`` and no turn is gated. See designs/CODEX_LLMQ_GATE_AND_TURBO.md.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

_logger = logging.getLogger(__name__)

#: Harness names whose turns run on the Codex subscription.
CODEX_HARNESSES = frozenset({"codex", "codex-native"})
#: llmq refusals decided on fresh evidence. Any other refusal fails open.
REFUSAL_REASONS = frozenset({"credit_safety_margin", "quota_exhausted"})
_DEFAULT_MODEL = "gpt-6.1-sol"
_DEFAULT_WORKSTREAM = "agent-infra"
_DEFAULT_RECHECK_S = 30.0
_DEFAULT_MAX_ADMISSION_DELAY_S = 5.0
_REQUEST_TIMEOUT_S = 5.0
_MODEL_PREFIXES = ("codex-subscription/", "omnigent-openai/system.ai.", "openai/")


@dataclass(frozen=True)
class GateConfig:
    """Where the runner reaches llmq, read once from the runner's environment.

    :param controller_url: llmq base URL, e.g. ``"http://100.87.142.18:17645"``.
    :param token_file: File holding the bearer token of an ``agent``-role grant
        whose actor is ``agent_id``; re-read on every call so a rotation needs
        no restart.
    :param agent_id: This host's llmq actor, e.g. ``"fleet-p16v"``.
    :param workstream: llmq workstream the turns are booked under.
    :param default_model: Model sent when the session names none.
    :param recheck_seconds: How often a held turn asks again.
    :param max_admission_delay_seconds: Cap on honouring llmq's pacing delay
        before dispatching an admitted turn.
    """

    controller_url: str
    token_file: Path
    agent_id: str
    workstream: str = _DEFAULT_WORKSTREAM
    default_model: str = _DEFAULT_MODEL
    recheck_seconds: float = _DEFAULT_RECHECK_S
    max_admission_delay_seconds: float = _DEFAULT_MAX_ADMISSION_DELAY_S

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GateConfig | None:
        """Build the config, or ``None`` when the gate is not provisioned here."""
        source: Mapping[str, str] = os.environ if env is None else env
        url = (source.get("LLMQ_CONTROLLER_URL") or "").strip().rstrip("/")
        token_file = (source.get("LLMQ_TOKEN_FILE") or "").strip()
        agent_id = (source.get("LLMQ_AGENT_ID") or "").strip()
        if not url or not token_file or not agent_id:
            return None

        def _seconds(name: str, default: float) -> float:
            raw = (source.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                return default
            return value if math.isfinite(value) and value >= 0 else default

        return cls(
            controller_url=url,
            token_file=Path(token_file),
            agent_id=agent_id,
            workstream=(source.get("LLMQ_WORKSTREAM") or "").strip() or _DEFAULT_WORKSTREAM,
            default_model=(source.get("LLMQ_MODEL") or "").strip() or _DEFAULT_MODEL,
            recheck_seconds=_seconds("LLMQ_GATE_RECHECK_SECONDS", _DEFAULT_RECHECK_S),
            max_admission_delay_seconds=_seconds(
                "LLMQ_MAX_ADMISSION_DELAY_SECONDS", _DEFAULT_MAX_ADMISSION_DELAY_S
            ),
        )

    def bearer(self) -> str | None:
        """The bearer token, or ``None`` when the file is unreadable or empty."""
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None


@dataclass(frozen=True)
class GateDecision:
    """What the runner does with a turn after asking llmq.

    :param allow: Dispatch the turn now (after ``admission_delay_seconds``).
    :param verified: llmq decided on fresh evidence; ``False`` means the turn
        runs without a usable check and the session shows the indicator.
    :param reason: llmq's reason slug, or the fault that made the gate fail open.
    :param resets_at: Earliest quota reset llmq reported with a refusal (epoch s).
    :param reservation_id: llmq reservation to settle at turn end, when any.
    :param credit_bearing: The turn runs on paid credits (Turbo past the margin).
    :param retry_after_seconds: llmq's pacing hint on a non-fresh refusal.
    :param admission_delay_seconds: llmq's pacing delay on an admission.
    """

    allow: bool
    verified: bool
    reason: str
    resets_at: float | None = None
    reservation_id: str | None = None
    credit_bearing: bool = False
    retry_after_seconds: float | None = None
    admission_delay_seconds: float = 0.0

    @property
    def state(self) -> str:
        if not self.allow:
            return "blocked"
        return "verified" if self.verified else "unverified"

    def message(self, *, turbo: bool) -> str | None:
        """The sentence the session shows, or ``None`` for a verified admission."""
        if self.allow and self.verified:
            return None
        if self.allow:
            tail = " Turbo is on, so paid credits may have been used." if turbo else ""
            return (
                f"Codex quota unverified ({_words(self.reason)}); "
                f"the turn ran without llmq's check.{tail}"
            )
        if self.reason == "credit_safety_margin":
            return (
                "Codex subscription quota is at the credit-safety margin (90%)"
                + _resets_clause(self.resets_at)
                + ". Enable Turbo on this session to use paid credits."
            )
        if self.reason == "quota_exhausted":
            return "Codex subscription quota is used up" + _resets_clause(self.resets_at) + "."
        return f"Codex turn held by llmq ({_words(self.reason)}); retrying."

    def status_payload(self, *, turbo: bool) -> dict[str, object]:
        """The ``quota_gate`` object for an ``external_session_status`` event."""
        payload: dict[str, object] = {
            "state": self.state,
            "reason": self.reason,
            "turbo": turbo,
            "credit_bearing": self.credit_bearing,
        }
        if self.resets_at is not None:
            payload["resets_at"] = self.resets_at
        message = self.message(turbo=turbo)
        if message:
            payload["message"] = message
        return payload


def _words(slug: str) -> str:
    return slug.replace("_", " ")


def _resets_clause(resets_at: float | None, *, now: float | None = None) -> str:
    if resets_at is None or not math.isfinite(resets_at):
        return ""
    current = time.time() if now is None else now
    minutes = max(0, int((resets_at - current) // 60))
    clock = time.strftime("%H:%M UTC", time.gmtime(resets_at))
    if minutes >= 120:
        relative = f"in {minutes // 60} h"
    else:
        relative = f"in {minutes} min"
    return f"; resets at {clock} ({relative})"


def unverified(reason: str) -> GateDecision:
    """An admission the gate could not verify: the turn runs, the session says so."""
    return GateDecision(allow=True, verified=False, reason=reason)


def turn_key(session_id: str, salt: str) -> str:
    """llmq's 64-hex turn identity for one runner turn."""
    return hashlib.sha256(f"{session_id}:{salt}".encode()).hexdigest()


def normalize_model(model: str | None, default: str) -> str:
    """Strip Omnigent catalog prefixes so llmq sees the Codex model slug."""
    value = (model or "").strip()
    for prefix in _MODEL_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value or default


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def decision_from_payload(data: object) -> GateDecision:
    """Classify llmq's admission body; anything unexpected fails open."""
    if not isinstance(data, dict):
        return unverified("llmq_unknown_shape")
    allowed = data.get("allowed")
    reason = data.get("reason")
    if not isinstance(allowed, bool) or not isinstance(reason, str) or not reason:
        return unverified("llmq_unknown_shape")
    if allowed:
        verified = data.get("verified", True) is not False
        unverified_reason = data.get("unverified_reason")
        reservation = data.get("reservation_id")
        return GateDecision(
            allow=True,
            verified=verified,
            reason=(
                unverified_reason
                if not verified and isinstance(unverified_reason, str) and unverified_reason
                else reason
            ),
            reservation_id=reservation if isinstance(reservation, str) and reservation else None,
            credit_bearing=data.get("credit_bearing") is True,
            admission_delay_seconds=_finite(data.get("admission_delay_seconds")) or 0.0,
        )
    if reason in REFUSAL_REASONS:
        return GateDecision(
            allow=False, verified=True, reason=reason, resets_at=_finite(data.get("resets_at"))
        )
    retry_after = _finite(data.get("retry_after_seconds"))
    if retry_after is not None:
        return GateDecision(
            allow=False, verified=True, reason=reason, retry_after_seconds=retry_after
        )
    # A refusal llmq gave no fresh-evidence reason or retry hint for: it could
    # not decide for this turn, so the turn runs with the indicator.
    return unverified(reason)


async def admit_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    *,
    turn_key: str,
    attempt: int,
    model: str,
    turbo: bool,
) -> GateDecision:
    """Ask llmq once; every fault is an unverified admission."""
    bearer = config.bearer()
    if bearer is None:
        return unverified("token_unavailable")
    body = {
        "turn_key": turn_key,
        "agent_id": config.agent_id,
        "workstream": config.workstream,
        "harness": "codex",
        "model": model,
        "attempt": attempt,
        "evidence": "controller",
        "turbo": turbo,
    }
    try:
        resp = await client.post(
            f"{config.controller_url}/v1/native-turns/admissions",
            json=body,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        _logger.warning("llmq gate: controller unreachable (%s); failing open", type(exc).__name__)
        return unverified("llmq_unreachable")
    if resp.status_code != 200:
        _logger.warning("llmq gate: controller answered HTTP %s; failing open", resp.status_code)
        return unverified(f"llmq_http_{resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        return unverified("llmq_invalid_response")
    return decision_from_payload(data)


async def settle_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    decision: GateDecision,
    *,
    turn_key: str,
    usage: Mapping[str, int] | None = None,
) -> None:
    """Settle the turn's reservation; zero usage keeps llmq's own estimate."""
    if decision.reservation_id is None:
        return
    bearer = config.bearer()
    if bearer is None:
        return
    payload = {
        "turn_key": turn_key,
        "reservation_id": decision.reservation_id,
        "usage": dict(usage) if usage else {"input_tokens": 0, "output_tokens": 0},
    }
    try:
        resp = await client.post(
            f"{config.controller_url}/v1/native-turns/settlements",
            json=payload,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError:
        _logger.debug("llmq gate: settlement unreachable for %s", decision.reservation_id)
        return
    if resp.status_code >= 400:
        _logger.debug(
            "llmq gate: settlement of %s answered HTTP %s",
            decision.reservation_id,
            resp.status_code,
        )


StatusCallback = Callable[[GateDecision, bool], Awaitable[None]]


async def gate_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    *,
    session_id: str,
    turn_salt: str,
    model: str,
    read_turbo: Callable[[], Awaitable[bool]],
    on_status: StatusCallback,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[GateDecision, str]:
    """Admit the turn, holding it while llmq refuses on fresh evidence.

    ``read_turbo`` is called before every attempt so a Turbo toggle made while
    the turn is held takes effect on the next check. ``on_status`` is called
    for every refusal, for an unverified admission, and for the admission that
    releases a held turn (so the session's blocked state clears).

    :returns: The final decision and the turn key to settle with.
    """
    key = turn_key(session_id, turn_salt)
    attempt = 0
    held = False
    while True:
        turbo = await read_turbo()
        decision = await admit_codex_turn(
            client, config, turn_key=key, attempt=attempt, model=model, turbo=turbo
        )
        if decision.allow:
            if decision.admission_delay_seconds > 0:
                await sleep(
                    min(decision.admission_delay_seconds, config.max_admission_delay_seconds)
                )
            if held or not decision.verified:
                await on_status(decision, turbo)
            return decision, key
        held = True
        await on_status(decision, turbo)
        wait = (
            decision.retry_after_seconds
            if decision.retry_after_seconds is not None
            else config.recheck_seconds
        )
        await sleep(max(1.0, min(wait, config.recheck_seconds)))
        attempt += 1
