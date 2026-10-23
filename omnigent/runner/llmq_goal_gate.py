"""llmq admission for Codex goal-mode continuation turns (review D3b).

Codex goal mode starts continuation turns by itself: when a thread with an
``active`` goal goes idle, Codex starts the next turn with a continuation
steering item. No runner ``turn/start`` precedes such a turn, so the runner's
pre-turn gate (:mod:`omnigent.runner.llmq_gate`) never sees it, and past the
credit-safety margin with Turbo off it would spend paid credits with no hold
and no indicator.

The codex-native forwarder runs in the runner process and sees every
``turn/started``. For a turn nobody admitted beforehand (see
:func:`~omnigent.runner.llmq_gate.turn_admission_record`) on a thread whose goal
is ``active``, :class:`GoalContinuationGate`:

* asks llmq's native-turn admission (a turn key per Codex turn id) with the
  session's current Turbo flag, exactly as the runner gate does;
* verified admission -> the turn runs; its reservation is activated at once (the
  turn is already running) and closed at the turn's end with the turn's measured
  usage (summed over its model responses), else ``usage_unmeasured``;
* fault, undecidable evidence or ledger throttling -> the turn runs and the
  session shows the unverified indicator (a running turn cannot wait);
* fresh refusal (margin, exhaustion) -> HOLD: pause the goal
  (``thread/goal/set status=paused``, so Codex does not start the next
  continuation), interrupt the turn, and show the held status with
  ``resets_at`` and the Turbo hint; re-check every ``recheck_seconds`` with a
  fresh attempt and the current Turbo flag; once admitted (e.g. Turbo turned
  on), resume the goal (``status=active``), and the continuation Codex starts
  then uses that admission. The goal's objective and the thread's context are
  never changed. A user who resumes or clears the goal while it is held ends the
  hold (a resumed continuation is admitted again). If the goal cannot be paused,
  the turn is left running with the ``ungated_continuation`` indicator:
  interrupting it without a pause would only make Codex start the next
  continuation at once.

Every Codex or llmq call and the Turbo read are bounded (5 s) and fail open; status
posts use the forwarder's server client and its own timeout. The forwarder's event
loop never waits on this module (each ``turn/started`` is handled in a task).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from omnigent.inner.codex_executor import CodexTurnUsage
from omnigent.runner.llmq_gate import (
    REFUSAL_REASONS,
    GateConfig,
    GateDecision,
    TurnActivation,
    TurnStreamTap,
    activate_codex_turn,
    admit_codex_turn,
    cancel_codex_turn,
    close_codex_turn,
    llmq_usage,
    normalize_model,
    turn_admission_record,
    turn_key,
    unverified,
)

_logger = logging.getLogger(__name__)

_CODEX_TIMEOUT_S = 5.0
#: How long a turn that may be the in-flight admitted ``turn/start`` waits for
#: the executor to record its id before it is treated as unadmitted.
_ATTRIBUTION_WAIT_S = 3.0
_ATTRIBUTION_POLL_S = 0.1
#: How long a released hold's admission waits for Codex to start the turn.
_GRANT_TTL_S = 120.0
#: How long the first held status waits for the interrupted turn to end, so the
#: turn's own idle edge does not land after (and over) it.
_INTERRUPT_SETTLE_S = 10.0
_FINISHED_KEEP = 16


class CodexRequester(Protocol):
    """The part of the Codex app-server client the gate uses."""

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class _Tracked:
    session_id: str
    decision: GateDecision
    key: str
    activation: TurnActivation


async def _codex_result(
    codex: CodexRequester, method: str, params: dict[str, Any]
) -> dict[str, Any] | None:
    """One bounded app-server request; ``None`` on any fault or error answer."""
    try:
        response = await asyncio.wait_for(codex.request(method, params), _CODEX_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — every Codex fault fails open
        _logger.warning("llmq goal gate: Codex %s failed (%s)", method, type(exc).__name__)
        return None
    result = response.get("result") if isinstance(response, dict) else None
    return result if isinstance(result, dict) else {}


_GOAL_FAULT = "<codex fault>"  # not a Codex goal status: Codex could not say


async def _goal_status(codex: CodexRequester, thread_id: str) -> str | None:
    """The thread's goal status; ``None`` without a goal, ``_GOAL_FAULT`` on a fault."""
    result = await _codex_result(codex, "thread/goal/get", {"threadId": thread_id})
    if result is None:
        return _GOAL_FAULT
    goal = result.get("goal")
    status = goal.get("status") if isinstance(goal, dict) else None
    return status if isinstance(status, str) else None


async def _read_turbo(server: httpx.AsyncClient, session_id: str) -> bool:
    """The session's current Turbo flag; off when the server cannot say."""
    try:
        resp = await server.get(f"/v1/sessions/{session_id}", timeout=_CODEX_TIMEOUT_S)
        return resp.status_code == 200 and resp.json().get("turbo") is True
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


async def _post_status(
    server: httpx.AsyncClient, session_id: str, decision: GateDecision, turbo: bool
) -> None:
    """The same ``quota_gate`` status the runner gate posts for its turns.

    Always ``running``, as for a held runner turn (the held sentence rides as
    ``blocked_on``). Never ``idle``: an extra idle edge would read as a native
    sub-agent's turn end and wake its parent; resuming the goal makes Codex
    start the continuation at once (``thread/goal/set`` active runs
    ``continue_if_idle``), and that turn's own edges follow.
    """
    from omnigent._native_post_delivery import post_external_session_status

    _logger.log(
        logging.INFO if decision.allow and decision.verified else logging.WARNING,
        "llmq goal gate session=%s state=%s reason=%s turbo=%s",
        session_id,
        decision.state,
        decision.reason,
        turbo,
        extra={"session_id": session_id},
    )
    try:
        await post_external_session_status(
            server,
            session_id=session_id,
            status="running",
            blocked_on=decision.message(turbo=turbo) if not decision.allow else None,
            quota_gate=decision.status_payload(turbo=turbo),
        )
    except (httpx.HTTPError, RuntimeError):
        _logger.debug("llmq goal gate: status post failed", exc_info=True)


class GoalContinuationGate:
    """Admits the Codex turns of one forwarder that nobody admitted before they started.

    The forwarder calls :meth:`turn_started`, :meth:`usage_updated` and
    :meth:`turn_finished` from its event loop (all non-blocking), and
    :meth:`aclose` when it stops.

    :param config: The runner gate's llmq configuration.
    :param http: llmq HTTP client (one is created on first use when ``None``).
    :param sleep: Sleep used between held re-checks (tests pass a fake).
    """

    def __init__(
        self,
        config: GateConfig,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        attribution_wait_seconds: float = _ATTRIBUTION_WAIT_S,
        grant_ttl_seconds: float = _GRANT_TTL_S,
        interrupt_settle_seconds: float = _INTERRUPT_SETTLE_S,
    ) -> None:
        self._config = config
        self._http = http
        self._owns_http = http is None
        self._sleep = sleep
        self._attribution_wait = attribution_wait_seconds
        self._grant_ttl = grant_ttl_seconds
        self._interrupt_settle = interrupt_settle_seconds
        # Off after the first "retained"/"released" close: that llmq cannot settle
        # an activated turn unmeasured, so activating would strand estimates.
        self._activate = True
        self._tasks: set[asyncio.Task[Any]] = set()
        # Long-lived waits (held re-checks, unused-admission expiry).
        self._waits: set[asyncio.Task[Any]] = set()
        self._tracked: dict[str, _Tracked] = {}
        self._finished: collections.OrderedDict[str, dict[str, object] | None] = (
            collections.OrderedDict()
        )
        self._grants: dict[str, tuple[GateDecision, str]] = {}
        self._holds: dict[str, asyncio.Task[None]] = {}
        # The turn being measured (turns of one thread run one at a time).
        self._meter = CodexTurnUsage()
        self._meter_turn: str | None = None
        self._turn_ended: dict[str, asyncio.Event] = {}

    @classmethod
    def from_env(cls) -> GoalContinuationGate | None:
        """The gate where the runner gate is provisioned, else ``None`` (inert)."""
        config = GateConfig.from_env()
        return cls(config) if config is not None else None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        return self._http

    def _spawn(
        self, coro: Coroutine[Any, Any, Any], name: str, *, wait: bool = False
    ) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        tasks = self._waits if wait else self._tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    # ----------------------------------------------------------- forwarder hooks

    def turn_started(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        bridge_dir: Path,
        server: httpx.AsyncClient,
        codex: CodexRequester | None,
        model: str | None,
    ) -> None:
        """A ``turn/started`` for the forwarder's thread: classify and admit it off-loop."""
        if not turn_id or not thread_id:
            return
        # Turns of a thread run one at a time: a still-tracked earlier turn ended
        # without a terminal event reaching us (reconnect), so close it unmeasured.
        for earlier in [t for t, tr in self._tracked.items() if tr.session_id == session_id]:
            self._close_later(earlier, None)
        self._meter.reset()
        self._meter_turn = turn_id
        self._turn_ended[turn_id] = asyncio.Event()
        while len(self._turn_ended) > _FINISHED_KEEP:
            self._turn_ended.pop(next(iter(self._turn_ended)))
        if codex is None:
            return
        self._spawn(
            self._admit_started_turn(
                session_id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                bridge_dir=bridge_dir,
                server=server,
                codex=codex,
                model=normalize_model(model, self._config.default_model),
            ),
            name=f"llmq-goal-gate-{session_id}",
        )

    def usage_updated(self, params: object) -> None:
        """A ``thread/tokenUsage/updated`` for the forwarder's thread."""
        turn_id = params.get("turnId") if isinstance(params, dict) else None
        counts = self._meter_turn is not None and (
            not isinstance(turn_id, str) or not turn_id or turn_id == self._meter_turn
        )
        self._meter.observe(params, count=counts)

    def turn_finished(self, turn_id: str | None) -> None:
        """A ``turn/completed`` / ``turn/failed`` for the forwarder's thread."""
        if not turn_id:
            return
        usage = None
        if turn_id == self._meter_turn:
            usage = self._meter.take(None)
            self._meter_turn = None
        ended = self._turn_ended.get(turn_id)
        if ended is not None:
            ended.set()
        if turn_id in self._tracked:
            self._close_later(turn_id, usage)
            return
        # Its admission may still be in flight: keep the usage for _track.
        self._finished[turn_id] = usage
        while len(self._finished) > _FINISHED_KEEP:
            self._finished.popitem(last=False)

    async def drain(self) -> None:
        """Wait for the in-flight work, not the long-lived waits (tests)."""
        while pending := list(self._tasks):
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self) -> None:
        """Stop holding, and end every reservation this gate still owns."""
        for task in list(self._waits):
            task.cancel()
        for turn_id in list(self._tracked):
            self._close_later(turn_id, None)
        for session_id in list(self._grants):
            self._drop_grant(session_id)
        pending = list(self._tasks)
        if pending:
            await asyncio.wait(pending, timeout=2 * _CODEX_TIMEOUT_S + 1)
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    # ----------------------------------------------------------- admission

    async def _attributed(self, bridge_dir: Path, turn_id: str) -> bool:
        """Whether the turn was admitted before it started (the runner, a hook)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._attribution_wait
        while True:
            record = turn_admission_record(bridge_dir, turn_id)
            if record != "pending" or loop.time() >= deadline:
                return record == "admitted"
            await asyncio.sleep(_ATTRIBUTION_POLL_S)

    async def _admit_started_turn(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        bridge_dir: Path,
        server: httpx.AsyncClient,
        codex: CodexRequester,
        model: str,
    ) -> None:
        try:
            if await self._attributed(bridge_dir, turn_id):
                return
            if await _goal_status(codex, thread_id) != "active":
                # Not a goal continuation (or the goal cannot be read): out of
                # this gate's scope; such turns are left as they were.
                return
            grant = self._grants.pop(session_id, None)
            if grant is not None:
                decision, key = grant
            else:
                turbo = await _read_turbo(server, session_id)
                key = turn_key(session_id, f"codex-turn:{turn_id}")
                decision = await admit_codex_turn(
                    self._client(), self._config, turn_key=key, attempt=0, model=model, turbo=turbo
                )
                if not decision.allow and decision.reason in REFUSAL_REASONS:
                    await self._hold(
                        session_id=session_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        server=server,
                        codex=codex,
                        model=model,
                        key=key,
                        decision=decision,
                        turbo=turbo,
                    )
                    return
                if not decision.allow:
                    # Ledger throttling: a turn that is already running cannot
                    # wait it out (design #76 caps such waits and then runs).
                    decision = unverified("llmq_" + decision.reason)
                if not decision.verified:
                    await _post_status(server, session_id, decision, turbo)
            if decision.reservation_id is not None:
                self._track(session_id, turn_id, decision, key)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the gate never blocks a turn on its own faults
            _logger.warning("llmq goal gate failed open for session=%s", session_id, exc_info=True)
            await _post_status(server, session_id, unverified("gate_error"), False)

    def _track(self, session_id: str, turn_id: str, decision: GateDecision, key: str) -> None:
        activation = TurnActivation(
            lambda: activate_codex_turn(self._client(), self._config, decision, turn_key=key)
        )
        if self._activate:
            activation()  # the turn is already running
        self._tracked[turn_id] = _Tracked(session_id, decision, key, activation)
        if turn_id in self._finished:
            self._close_later(turn_id, self._finished.pop(turn_id))

    def _close_later(self, turn_id: str, usage: dict[str, object] | None) -> None:
        tracked = self._tracked.pop(turn_id, None)
        if tracked is not None:
            self._spawn(self._close(tracked, usage), name=f"llmq-goal-close-{tracked.session_id}")

    async def _close(self, tracked: _Tracked, usage: dict[str, object] | None) -> None:
        tap = TurnStreamTap()
        tap.dispatched = True  # the turn ran: Codex reported it started
        tap.usage = llmq_usage(usage)
        outcome = await close_codex_turn(
            self._client(),
            self._config,
            tracked.decision,
            turn_key=tracked.key,
            tap=tap,
            activated=await tracked.activation.result(),
        )
        if outcome in ("retained", "released") and self._activate:
            self._activate = False
            _logger.warning(
                "llmq goal gate: llmq has no unmeasured settlement; activation turned off"
            )
        _logger.info(
            "llmq goal gate session=%s reservation %s (%s)",
            tracked.session_id,
            outcome,
            tracked.decision.reservation_id,
            extra={"session_id": tracked.session_id},
        )

    # ----------------------------------------------------------- hold

    async def _hold(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        server: httpx.AsyncClient,
        codex: CodexRequester,
        model: str,
        key: str,
        decision: GateDecision,
        turbo: bool,
    ) -> None:
        paused = await _codex_result(
            codex, "thread/goal/set", {"threadId": thread_id, "status": "paused"}
        )
        if paused is None:
            await _post_status(server, session_id, unverified("ungated_continuation"), turbo)
            return
        await _codex_result(codex, "turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        ended = self._turn_ended.get(turn_id)
        if ended is not None:
            # Post after the interrupted turn's own idle edge, not under it.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ended.wait(), self._interrupt_settle)
        await _post_status(server, session_id, decision, turbo)
        running = self._holds.get(session_id)
        if running is None or running.done():
            self._holds[session_id] = self._spawn(
                self._hold_loop(
                    session_id=session_id,
                    thread_id=thread_id,
                    server=server,
                    codex=codex,
                    model=model,
                    key=key,
                ),
                name=f"llmq-goal-hold-{session_id}",
                wait=True,
            )

    async def _hold_loop(
        self,
        *,
        session_id: str,
        thread_id: str,
        server: httpx.AsyncClient,
        codex: CodexRequester,
        model: str,
        key: str,
    ) -> None:
        attempt = 0
        try:
            while True:
                await self._sleep(self._config.recheck_seconds)
                status = await _goal_status(codex, thread_id)
                if status == _GOAL_FAULT:
                    # Codex could not say: the goal is still paused by us, so keep
                    # holding (ending here would strand it paused for good).
                    continue
                if status != "paused":
                    # Resumed or cleared by someone else: the hold is over (a
                    # resumed continuation is admitted again at turn/started).
                    return
                attempt += 1
                turbo = await _read_turbo(server, session_id)
                decision = await admit_codex_turn(
                    self._client(),
                    self._config,
                    turn_key=key,
                    attempt=attempt,
                    model=model,
                    turbo=turbo,
                )
                if not decision.allow and decision.reason in REFUSAL_REASONS:
                    await _post_status(server, session_id, decision, turbo)
                    continue
                if not decision.allow:
                    decision = unverified("llmq_" + decision.reason)
                if decision.reservation_id is not None:
                    self._grants[session_id] = (decision, key)
                    self._spawn(
                        self._expire_grant(session_id, decision),
                        name=f"llmq-goal-grant-{session_id}",
                        wait=True,
                    )
                # Clears the held state; the continuation runs on this admission.
                await _post_status(server, session_id, decision, turbo)
                resumed = await _codex_result(
                    codex, "thread/goal/set", {"threadId": thread_id, "status": "active"}
                )
                if resumed is None:
                    self._drop_grant(session_id)
                return
        finally:
            if self._holds.get(session_id) is asyncio.current_task():
                self._holds.pop(session_id, None)

    async def _expire_grant(self, session_id: str, decision: GateDecision) -> None:
        await asyncio.sleep(self._grant_ttl)
        grant = self._grants.get(session_id)
        if grant is not None and grant[0] is decision:
            self._drop_grant(session_id)

    def _drop_grant(self, session_id: str) -> None:
        """Release an admission no turn used (it never ran)."""
        grant = self._grants.pop(session_id, None)
        if grant is None:
            return
        decision, key = grant
        self._spawn(
            cancel_codex_turn(self._client(), self._config, decision, turn_key=key),
            name=f"llmq-goal-grant-cancel-{session_id}",
        )
