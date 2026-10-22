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
llmq actor (``LLMQ_AGENT_ID``) AND the launch's quota route is ``personal-llmq``
(``OMNIGENT_QUOTA_ROUTE``; a ``work-vertex`` or unclassified launch is never gated);
otherwise :func:`GateConfig.from_env` returns ``None`` and no turn is gated.
See designs/CODEX_LLMQ_GATE_AND_TURBO.md.

Reservation lifecycle (llmq has no "usage unmeasured" settlement, and a zero-token
settlement would be stored as measured usage and feed its token-cost calibration):

* the turn never reached the harness (spawn failure, rejected delivery, interrupt
  before the first event) -> cancel the reservation, so the lease does not leak;
* the turn ran and the harness reported real token usage -> settle with it;
* the turn ran but usage is unknown -> cancel the reservation unmeasured. This is
  deliberate under-reporting (llmq is not a security boundary and fails open), so
  unmeasured estimates cannot pile up into false holds: see :func:`close_codex_turn`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.host.project_attribution import PERSONAL_QUOTA_ROUTE, QUOTA_ROUTE_ENV_VAR
from omnigent.model_fallbacks import CODEX_DEFAULT_MODEL

_logger = logging.getLogger(__name__)

#: Harness names whose turns run on the Codex subscription.
CODEX_HARNESSES = frozenset({"codex", "codex-native"})
#: llmq refusals decided on fresh evidence. Any other refusal fails open.
REFUSAL_REASONS = frozenset({"credit_safety_margin", "quota_exhausted"})
#: What the Codex harness itself launches with when nothing names a model.
_DEFAULT_MODEL = CODEX_DEFAULT_MODEL
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
    :param default_model: Model sent when neither the session nor its agent spec
        names one (the Codex harness's own launch default).
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
        """Build the config, or ``None`` when the gate is not provisioned here.

        Also ``None`` unless the launch's quota route is ``personal-llmq``: the
        Codex sessions of a ``work-vertex`` (or unclassified) launch must never
        be metered against the personal llmq.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        if (source.get(QUOTA_ROUTE_ENV_VAR) or "").strip() != PERSONAL_QUOTA_ROUTE:
            return None
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
        except (OSError, UnicodeDecodeError):
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


def effective_codex_model(model_override: str | None, spec_model: str | None, default: str) -> str:
    """The Codex model slug llmq is asked about, resolved as the harness resolves it.

    The per-turn ``/model`` override wins, then the agent spec's pinned model, then
    the harness's launch default. The turn message's ``model`` field is NOT an
    input: Omnigent fills it with the agent *name* (a display tag), which llmq
    rejects as an unsupported native model.
    """
    return normalize_model(model_override or spec_model, default)


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


def llmq_usage(raw: object) -> dict[str, int] | None:
    """Map an Omnigent ``response.completed`` usage object to llmq's native usage.

    Omnigent reports the non-cached input in ``input_tokens`` and cache reads
    separately; llmq's ``input_tokens`` is the whole turn input with
    ``cached_input_tokens`` a subset of it. Returns ``None`` unless the harness
    reported real consumption: an absent object, non-integers, or zero input and
    output tokens all mean "not measured" (a turn that ran always read input), and
    must never reach llmq as measured zeros.
    """
    if not isinstance(raw, Mapping):
        return None

    def _count(name: str) -> int | None:
        value = raw.get(name)
        if value is None:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    fresh, output, cached = (
        _count("input_tokens"),
        _count("output_tokens"),
        _count("cache_read_input_tokens"),
    )
    if fresh is None or output is None or cached is None:
        return None
    total_input = fresh + cached
    if total_input + output == 0:
        return None
    usage = {"input_tokens": total_input, "output_tokens": output}
    if cached:
        usage["cached_input_tokens"] = cached
    return usage


class TurnStreamTap:
    """Watches a turn's SSE stream for dispatch and measured usage.

    The runner feeds it every chunk of the turn's response while draining it.
    ``dispatched`` becomes true once the harness produced its first event
    (``response.created``); ``usage`` holds llmq-shaped usage once a
    ``response.completed`` carried real token counts. Parsing is best-effort and
    never raises: a tap that understands nothing leaves both unset, which makes the
    reservation "ran, usage unknown".
    """

    _MAX_PENDING = 1 << 20

    def __init__(self) -> None:
        self.dispatched = False
        self.usage: dict[str, int] | None = None
        self._pending = ""

    def feed(self, chunk: bytes | str) -> None:
        text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        self._pending += text
        *lines, self._pending = self._pending.split("\n")
        if len(self._pending) > self._MAX_PENDING:
            self._pending = ""
        for line in lines:
            if line.startswith("data:"):
                self._event(line[5:].strip())

    def _event(self, payload: str) -> None:
        if not payload.startswith("{"):
            return
        try:
            event = json.loads(payload)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "response.created":
            self.dispatched = True
        elif kind == "response.completed":
            self.dispatched = True
            response = event.get("response")
            if isinstance(response, dict):
                measured = llmq_usage(response.get("usage"))
                if measured is not None:
                    self.usage = measured


async def settle_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    decision: GateDecision,
    *,
    turn_key: str,
    usage: Mapping[str, int],
) -> bool:
    """Settle the turn's reservation with MEASURED usage.

    There is deliberately no default usage: llmq stores whatever is sent as
    measured tokens, so a placeholder would poison its calibration and zero the
    charge once measured charging is on. Callers without real counts use
    :func:`close_codex_turn`, which cancels the reservation unmeasured instead.

    :returns: Whether llmq accepted the settlement.
    """
    if decision.reservation_id is None:
        return False
    return await _post_reservation(
        client,
        config,
        "settlements",
        {
            "turn_key": turn_key,
            "reservation_id": decision.reservation_id,
            "usage": dict(usage),
        },
    )


async def cancel_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    decision: GateDecision,
    *,
    turn_key: str,
) -> bool:
    """Release a reservation whose turn never reached the harness.

    :returns: Whether llmq accepted the cancellation.
    """
    if decision.reservation_id is None:
        return False
    return await _post_reservation(
        client,
        config,
        "cancellations",
        {"turn_key": turn_key, "reservation_id": decision.reservation_id},
    )


async def _post_reservation(
    client: httpx.AsyncClient,
    config: GateConfig,
    route: str,
    payload: dict[str, object],
) -> bool:
    bearer = config.bearer()
    if bearer is None:
        return False
    try:
        resp = await client.post(
            f"{config.controller_url}/v1/native-turns/{route}",
            json=payload,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError:
        _logger.debug("llmq gate: %s unreachable for %s", route, payload.get("reservation_id"))
        return False
    if resp.status_code >= 400:
        _logger.debug(
            "llmq gate: %s of %s answered HTTP %s",
            route,
            payload.get("reservation_id"),
            resp.status_code,
        )
        return False
    return True


async def close_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    decision: GateDecision,
    *,
    turn_key: str,
    tap: TurnStreamTap,
) -> str:
    """End an admitted turn's reservation according to what the stream showed.

    :returns: ``"cancelled"`` (the turn never reached the harness),
        ``"settled"`` (real usage reported), ``"released"`` (the turn ran but its
        usage is unknown, so the reservation was cancelled unmeasured), or
        ``"none"`` when there was no reservation or llmq refused the call.
    """
    if decision.reservation_id is None:
        return "none"
    if not tap.dispatched:
        ok = await cancel_codex_turn(client, config, decision, turn_key=turn_key)
        return "cancelled" if ok else "none"
    if tap.usage is not None:
        ok = await settle_codex_turn(client, config, decision, turn_key=turn_key, usage=tap.usage)
        return "settled" if ok else "none"
    # The turn RAN but nothing measured it (every codex-native turn: its executor
    # reports no usage). llmq has no "unmeasured completion" settlement, and a
    # zero-token settlement would be stored as measured usage. Leaving the
    # reservation pending is not neutral either: llmq counts every active
    # reservation's estimate in the window's `pending` (engine.py `_pending_usage`,
    # read by `_evaluate_window` as `global_available = capacity - used - pending`)
    # until the 86,400 s lease expires, so unmeasured turns would pile up and make
    # llmq answer `provider_window_exhausted` / pacing holds against a window the
    # provider says is nearly empty. Cancel it instead.
    #
    # This under-reports on purpose: the cancelled turn's cost is not booked until
    # the provider's own usedPercent shows it in the next receipt. oz's rule is
    # that llmq is NOT a security boundary and fails OPEN with an indicator, so a
    # spurious hold from our own estimates is the worse error than a slightly late
    # count. The real 90% credit-safety margin is unaffected: it is read from the
    # provider-reported usedPercent (codex_native_contract.py), not from reservations.
    # llmq accepts the cancel because the gate never calls /activations, so the
    # reservation is not marked dispatch-authorized (engine.NativeTurnDispatched).
    ok = await cancel_codex_turn(client, config, decision, turn_key=turn_key)
    return "released" if ok else "none"


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

    If the turn is interrupted (or anything raises) after llmq admitted it but
    before this returns, the reservation is cancelled so its lease does not leak.

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
            try:
                if decision.admission_delay_seconds > 0:
                    await sleep(
                        min(decision.admission_delay_seconds, config.max_admission_delay_seconds)
                    )
                if held or not decision.verified:
                    await on_status(decision, turbo)
            except BaseException:
                # Admitted but never dispatched: release the lease, shielded so an
                # interrupt (CancelledError) cannot abort the release itself.
                await asyncio.shield(cancel_codex_turn(client, config, decision, turn_key=key))
                raise
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
