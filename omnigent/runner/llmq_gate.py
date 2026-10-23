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

Reservation lifecycle (a zero-token settlement would be stored as measured usage
and feed llmq's token-cost calibration, so it is never sent):

* the harness started the turn (its first stream event) -> activate the
  reservation (``/activations``), which records it as dispatched; after that llmq
  refuses to cancel it. Activation is bookkeeping: it never holds or fails a turn
  that is already running (see :func:`activate_codex_turn`);
* the turn never reached the harness (spawn failure, rejected delivery, interrupt
  before the first event) -> cancel the reservation, so the lease does not leak
  (it was never activated);
* the turn ran and the harness reported real token usage -> settle with it;
* the turn ran but usage is unknown -> settle it ``usage_unmeasured`` at llmq's own
  estimate (marked ``retained_estimate``; ozzieba/chatgpt-playground#1328). An llmq
  without that form answers 422: the reservation is then cancelled if it was not
  activated, else left at its estimate for the lease (llmq's own conservative
  charge for a dispatched turn). Any other llmq fault leaves it for the lease to
  expire: see :func:`close_codex_turn`.

Ledger throttling (pacing, share, window estimate) is waited out for at most
``LLMQ_MAX_ADMISSION_DELAY_SECONDS`` in total, then the turn runs with the
``llmq_<reason>`` indicator; only the credit-safety margin / exhaustion refusals hold.
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
from dataclasses import dataclass, field, replace
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
    :param model: The model the admission asked about; an activation repeats it
        (llmq re-checks the reservation against the same model and Turbo flag).
    :param turbo: The Turbo flag the admission carried.
    :param attempt: The admission attempt that produced this decision.
    """

    allow: bool
    verified: bool
    reason: str
    resets_at: float | None = None
    reservation_id: str | None = None
    credit_bearing: bool = False
    retry_after_seconds: float | None = None
    admission_delay_seconds: float = 0.0
    model: str | None = field(default=None, compare=False)
    turbo: bool = field(default=False, compare=False)
    attempt: int = field(default=0, compare=False)

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
    return replace(decision_from_payload(data), model=model, turbo=turbo, attempt=attempt)


def _activation_id(reservation_id: str) -> str:
    """A stable activation id per reservation, so a repeated call replays."""
    return "omnigent-" + hashlib.sha256(reservation_id.encode()).hexdigest()[:32]


async def activate_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    decision: GateDecision,
    *,
    turn_key: str,
) -> bool | None:
    """Mark an admitted turn's reservation dispatched (``/activations``).

    Called once the harness has started the turn, so it is bookkeeping, not a
    second gate: llmq records ``dispatched_at`` / ``dispatch_authorized_at`` (after
    which it refuses to cancel the reservation, because the turn may have spent
    tokens), and the result never holds or fails the running turn. The body
    repeats the admission (llmq validates it as ``NativeTurnActivation``: the same
    model and Turbo flag, so a credit-bearing Turbo admission re-checks as one).
    Same fault policy as admission: 5 s timeout, no retry, any fault is logged.

    :returns: ``True`` when llmq authorised the dispatch (``ready``), ``False``
        when it answered not ready (e.g. its receipt went stale since the
        admission; the reservation then stays cancellable), ``None`` when there
        was nothing to activate or llmq could not be asked.
    """
    if decision.reservation_id is None or not decision.verified or decision.model is None:
        return None
    bearer = config.bearer()
    if bearer is None:
        return None
    body = {
        "turn_key": turn_key,
        "agent_id": config.agent_id,
        "workstream": config.workstream,
        "harness": "codex",
        "model": decision.model,
        "attempt": decision.attempt,
        "evidence": "controller",
        "turbo": decision.turbo,
        "reservation_id": decision.reservation_id,
        "activation_id": _activation_id(decision.reservation_id),
    }
    try:
        resp = await client.post(
            f"{config.controller_url}/v1/native-turns/activations",
            json=body,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        _logger.warning(
            "llmq gate: activation of %s unreachable (%s)",
            decision.reservation_id,
            type(exc).__name__,
        )
        return None
    if resp.status_code != 200:
        _logger.warning(
            "llmq gate: activation of %s answered HTTP %s",
            decision.reservation_id,
            resp.status_code,
        )
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    ready = data.get("ready") if isinstance(data, dict) else None
    if not isinstance(ready, bool):
        return None
    if not ready:
        _logger.info(
            "llmq gate: activation of %s not ready (%s); the turn is already running",
            decision.reservation_id,
            data.get("reason"),
        )
    return ready


class TurnActivation:
    """Starts an admitted turn's activation once, when the harness starts the turn.

    Use it as a :class:`TurnStreamTap` ``on_dispatch`` callback: the first call
    schedules ``start`` as a task (the tap is fed synchronously, inside the event
    loop); :meth:`result` awaits it before the reservation is closed, so the close
    sees whether llmq recorded the dispatch.
    """

    def __init__(self, start: Callable[[], Awaitable[bool | None]]) -> None:
        self._start = start
        self._task: asyncio.Task[bool | None] | None = None

    def __call__(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> bool | None:
        try:
            return await self._start()
        except Exception:  # noqa: BLE001 — activation is best-effort bookkeeping
            _logger.warning("llmq gate: activation failed", exc_info=True)
            return None

    async def result(self) -> bool:
        """Whether llmq authorised the dispatch (``False`` if never started)."""
        if self._task is None:
            return False
        return await asyncio.shield(self._task) is True


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
    (``response.created``), and ``on_dispatch`` (if given) is called once at that
    moment; ``usage`` holds llmq-shaped usage once a ``response.completed`` carried
    real token counts. Parsing is best-effort and never raises: a tap that
    understands nothing leaves both unset, which makes the reservation "never ran".
    """

    _MAX_PENDING = 1 << 20

    def __init__(self, on_dispatch: Callable[[], None] | None = None) -> None:
        self.dispatched = False
        self.usage: dict[str, int] | None = None
        self._pending = ""
        self._on_dispatch = on_dispatch

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
        except (ValueError, RecursionError):
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind in ("response.created", "response.completed"):
            self._mark_dispatched()
        if kind == "response.completed":
            response = event.get("response")
            if isinstance(response, dict):
                measured = llmq_usage(response.get("usage"))
                if measured is not None:
                    self.usage = measured

    def _mark_dispatched(self) -> None:
        if self.dispatched:
            return
        self.dispatched = True
        if self._on_dispatch is not None:
            try:
                self._on_dispatch()
            except Exception:  # noqa: BLE001 — the tap never raises into the drain
                _logger.warning("llmq gate: on_dispatch callback failed", exc_info=True)


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
    :func:`settle_unmeasured_codex_turn` (via :func:`close_codex_turn`).

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


async def settle_unmeasured_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    decision: GateDecision,
    *,
    turn_key: str,
) -> int | None:
    """Settle a turn that RAN but reported no usage, at llmq's own estimate.

    llmq records it as ``native_usage_basis=retained_estimate``: never as
    measured tokens, and never as a cancellation (which means "never ran" and
    refunds the pacing debit). The estimate then counts as pending only until
    the provider's next usage observation. Needs an llmq with the
    ``usage_unmeasured`` settlement (ozzieba/chatgpt-playground#1328).

    :returns: llmq's HTTP status, or ``None`` when it could not be asked.
    """
    if decision.reservation_id is None:
        return None
    return await _post_reservation_status(
        client,
        config,
        "settlements",
        {
            "turn_key": turn_key,
            "reservation_id": decision.reservation_id,
            "usage_unmeasured": True,
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
    status = await _post_reservation_status(client, config, route, payload)
    return status is not None and status < 400


async def _post_reservation_status(
    client: httpx.AsyncClient,
    config: GateConfig,
    route: str,
    payload: dict[str, object],
) -> int | None:
    bearer = config.bearer()
    if bearer is None:
        return None
    try:
        resp = await client.post(
            f"{config.controller_url}/v1/native-turns/{route}",
            json=payload,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError:
        _logger.debug("llmq gate: %s unreachable for %s", route, payload.get("reservation_id"))
        return None
    if resp.status_code >= 400:
        _logger.debug(
            "llmq gate: %s of %s answered HTTP %s",
            route,
            payload.get("reservation_id"),
            resp.status_code,
        )
    return resp.status_code


async def close_codex_turn(
    client: httpx.AsyncClient,
    config: GateConfig,
    decision: GateDecision,
    *,
    turn_key: str,
    tap: TurnStreamTap,
    activated: bool = False,
) -> str:
    """End an admitted turn's reservation according to what the stream showed.

    :param activated: llmq authorised the turn's dispatch (:func:`activate_codex_turn`
        answered ready), so it will refuse a cancellation (HTTP 409).
    :returns: ``"cancelled"`` (the turn never reached the harness),
        ``"settled"`` (real usage reported), ``"estimated"`` (the turn ran but
        its usage is unknown, so llmq settled it at its estimate, marked
        ``retained_estimate``), ``"released"`` (the same, against an llmq without
        that settlement form, so the reservation was cancelled instead),
        ``"retained"`` (the same, but the turn was activated, so it stays at its
        estimate until the lease ends), or ``"none"`` when there was no
        reservation or llmq refused the call.
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
    # reports no usage). A zero-token settlement would be stored as measured
    # usage, a cancellation means "never ran" (charging 0 and refunding the
    # pacing debit), and leaving the reservation active would count its estimate
    # as pending until the 86,400 s lease expires. Settle it unmeasured: llmq
    # charges the estimate, marks it `retained_estimate`, and stops counting it
    # as pending at the provider's next usage observation.
    status = await settle_unmeasured_codex_turn(client, config, decision, turn_key=turn_key)
    if status is not None and status < 400:
        return "estimated"
    if status == 422:
        if activated:
            # llmq refuses to cancel a dispatched turn (409: it may have spent
            # tokens); without the unmeasured form its only remaining charge is
            # the estimate held until the lease ends.
            _logger.warning(
                "llmq gate: %s ran unmeasured on an llmq without usage_unmeasured; "
                "its estimate stays pending until the lease ends",
                decision.reservation_id,
            )
            return "retained"
        # An llmq from before the `usage_unmeasured` form rejects the body. The
        # least-bad fallback there is to release the reservation (under-reporting
        # until the next receipt) rather than hold its estimate for the lease;
        # llmq is not a security boundary (oz, 2026-10-07).
        ok = await cancel_codex_turn(client, config, decision, turn_key=turn_key)
        return "released" if ok else "none"
    return "none"


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
    paced = 0.0
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
        if decision.reason not in REFUSAL_REASONS and decision.retry_after_seconds is not None:
            # A ledger denial (pacing, share, window estimate) is throttling, not
            # the fresh-evidence margin: design #76 waits for it at most
            # `max_admission_delay_seconds` in total and then runs the turn with
            # the indicator, instead of holding it without limit.
            wait = max(1.0, min(decision.retry_after_seconds, config.recheck_seconds))
            if paced + wait > config.max_admission_delay_seconds:
                decision = unverified("llmq_" + decision.reason)
                await on_status(decision, turbo)
                return decision, key
            paced += wait
            held = True
            await on_status(decision, turbo)
            await sleep(wait)
            attempt += 1
            continue
        held = True
        await on_status(decision, turbo)
        wait = (
            decision.retry_after_seconds
            if decision.retry_after_seconds is not None
            else config.recheck_seconds
        )
        await sleep(max(1.0, min(wait, config.recheck_seconds)))
        attempt += 1


# ----------------------------------------------------------------- turn attribution
#
# Which Codex turns were admitted BEFORE they started, per bridge directory, so the
# codex-native forwarder can tell them from turns Codex starts by itself (goal-mode
# continuations), which it admits at ``turn/started``
# (:mod:`omnigent.runner.llmq_goal_gate`). The codex-native executor records every
# turn it starts (the runner gated that dispatch); any other gate that knows Codex's
# turn id (a ``UserPromptSubmit`` hook gets ``turn_id``) can record its turns too.

#: One admitted Codex turn id per line.
ADMITTED_TURNS_FILE = "llmq-admitted-turns"
#: Present while an admitted ``turn/start`` is in flight (its turn id not known yet).
TURN_START_PENDING_FILE = "llmq-turn-start-pending"
_ADMITTED_TURNS_MAX_BYTES = 64 * 1024
_ADMITTED_TURNS_KEEP = 256
#: A pending marker older than this is a crashed start, not one in flight.
TURN_START_PENDING_MAX_AGE_S = 60.0


def mark_turn_start_pending(bridge_dir: Path) -> None:
    """Record that an admitted ``turn/start`` is about to be sent (best-effort)."""
    try:
        (bridge_dir / TURN_START_PENDING_FILE).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        _logger.debug("llmq gate: could not mark a pending turn start", exc_info=True)


def clear_turn_start_pending(bridge_dir: Path) -> None:
    """Drop the pending-start marker (best-effort)."""
    try:
        (bridge_dir / TURN_START_PENDING_FILE).unlink(missing_ok=True)
    except OSError:
        _logger.debug("llmq gate: could not clear a pending turn start", exc_info=True)


def note_admitted_turn(bridge_dir: Path, turn_id: str, *, clear_pending: bool = True) -> None:
    """Record that Codex turn ``turn_id`` was admitted before it started.

    Also clears the pending-start marker, unless ``clear_pending`` is false (a
    caller other than the runner's own ``turn/start`` must leave that marker to
    the runner). Best-effort: never raises.
    """
    if turn_id and "\n" not in turn_id:
        path = bridge_dir / ADMITTED_TURNS_FILE
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(turn_id + "\n")
            if path.stat().st_size > _ADMITTED_TURNS_MAX_BYTES:
                kept = path.read_text(encoding="utf-8").splitlines()[-_ADMITTED_TURNS_KEEP:]
                tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
                os.replace(tmp, path)
        except (OSError, ValueError):
            _logger.debug("llmq gate: could not record admitted turn %s", turn_id, exc_info=True)
    if clear_pending:
        clear_turn_start_pending(bridge_dir)


def turn_admission_record(bridge_dir: Path, turn_id: str) -> str:
    """What the bridge records about ``turn_id``.

    :returns: ``"admitted"`` (recorded), ``"pending"`` (not recorded, but an
        admitted ``turn/start`` is in flight, so it may be that one), or
        ``"unknown"`` (nobody admitted it).
    """
    try:
        recorded = (bridge_dir / ADMITTED_TURNS_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):  # ValueError: a corrupted (non-UTF-8) record
        recorded = ""
    if turn_id in recorded.splitlines():
        return "admitted"
    try:
        age = time.time() - float(
            (bridge_dir / TURN_START_PENDING_FILE).read_text(encoding="utf-8").strip()
        )
    except (OSError, ValueError):
        return "unknown"
    return "pending" if age < TURN_START_PENDING_MAX_AGE_S else "unknown"
