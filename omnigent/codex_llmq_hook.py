"""llmq admission for prompts typed into a terminal-UI Codex session (design #76, "B").

oz, 2026-10-06: "gate via llmq, but with a per session 'turbo' button on omnigent
right next to the model selector". oz, 2026-10-07: "this is not a security boundary;
in fact llmq should fail open but with an indicator".

The runner gates the turns it delivers (:mod:`omnigent.runner.llmq_gate`); a prompt
typed into the Codex TUI never passes through the runner. For terminal-UI sessions
(``omnigent.wrapper=codex-native-ui``) the generated ``CODEX_HOME/hooks.json``
therefore also registers this module (see
:func:`omnigent.codex_native_app_server._write_codex_policy_hooks_file`):

* ``UserPromptSubmit`` -> ``python -I -m omnigent.codex_llmq_hook prompt-submit``:
  asks llmq's native-turn admission once per prompt (a fresh turn key each time)
  with the session's current Turbo flag read from the server. A refusal on fresh
  evidence answers ``{"decision": "block", "reason": ...}``, which keeps the prompt
  in the TUI composer; anything else answers ``{}``. Every fault (llmq or server
  unreachable, 5 s timeout, HTTP error, unknown shape, missing files) fails open
  with the "quota unverified" indicator posted through the server, exactly like the
  runner gate (``quota_gate`` on the session's external status).
* ``Stop`` -> ``... stop``: settles the turn's reservation ``usage_unmeasured``
  (llmq's retained estimate, ozzieba/chatgpt-playground#1328; an llmq without that
  form answers 422 and the reservation is cancelled instead). Codex hooks carry no
  token usage, so TUI settlement is ALWAYS the retained estimate, never measured
  usage and never ``{0, 0}``. Codex fires ``Stop`` only for a turn that completed
  (not for a failed or hook-blocked one); a turn without one is settled the same
  way by the next prompt, and llmq's lease covers a session that ends first.

Turns the runner delivers into the same app-server thread (web messages of a
terminal-UI session) fire ``UserPromptSubmit`` too. The executor marks each
``turn/start`` it issues (:func:`mark_runner_turn`) and this hook skips the marked
turn, so a runner-gated turn is never admitted twice.

Codex runs hooks with the app-server's filtered environment
(:func:`omnigent.inner.codex_executor._clean_codex_env`), which keeps
``OMNIGENT_QUOTA_ROUTE`` and ``LLMQ_AGENT_ID`` but drops ``LLMQ_CONTROLLER_URL`` and
``LLMQ_TOKEN_FILE``. The launching process (runner or ``omnigent codex``) therefore
snapshots the gate variables into the bridge dir (:func:`write_gate_snapshot`: the
token FILE path, never the token) and registers the hooks only when
:meth:`GateConfig.from_env` accepts them; the hook applies the same rule to that
snapshot, and a hook environment naming any other quota route vetoes it.

Output is only ever ``{}`` or the block object: no tokens, URLs, or file paths, and
nothing on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent.host.project_attribution import PERSONAL_QUOTA_ROUTE, QUOTA_ROUTE_ENV_VAR

if TYPE_CHECKING:
    import httpx

    from omnigent.runner.llmq_gate import GateConfig, GateDecision

#: Module the hook commands run; also the trust marker for ``hooks/list``.
HOOK_MODULE = "omnigent.codex_llmq_hook"
#: Codex-side timeout (s) for each hook subprocess. The hook bounds itself well
#: below it (:data:`_HOOK_BUDGET_S`); Codex killing it would skip the indicator.
HOOK_TIMEOUT_SECONDS = 60
#: Environment names :meth:`GateConfig.from_env` reads (llmq_gate has no constant).
GATE_ENV_NAMES = (
    QUOTA_ROUTE_ENV_VAR,
    "LLMQ_CONTROLLER_URL",
    "LLMQ_TOKEN_FILE",
    "LLMQ_AGENT_ID",
    "LLMQ_WORKSTREAM",
    "LLMQ_MODEL",
    "LLMQ_GATE_RECHECK_SECONDS",
    "LLMQ_MAX_ADMISSION_DELAY_SECONDS",
)
_REQUIRED_ENV = ("LLMQ_CONTROLLER_URL", "LLMQ_TOKEN_FILE", "LLMQ_AGENT_ID")
_GATE_FILE = "llmq_gate.json"
_STATE_FILE = "llmq_gate_state.json"
_RUNNER_TURN_FILE = "llmq_runner_turn.json"
#: A runner ``turn/start`` marker older than this is ignored (the turn's hook
#: never fired, e.g. the hook was not trusted yet).
RUNNER_TURN_TTL_SECONDS = 300.0
_SERVER_TIMEOUT_S = 5.0
_REQUEST_TIMEOUT_S = 5.0
_HOOK_BUDGET_S = 45.0


# ----------------------------------------------------------------- launch side


def gate_env_snapshot(env: Mapping[str, str]) -> dict[str, str] | None:
    """The gate variables of *env*, or ``None`` when the gate is not provisioned.

    :param env: The launching process's environment, e.g. :data:`os.environ`.
    :returns: Only the :data:`GATE_ENV_NAMES` entries that are set, when
        :meth:`GateConfig.from_env` accepts *env*; else ``None``.
    """
    from omnigent.runner.llmq_gate import GateConfig

    if GateConfig.from_env(env) is None:
        return None
    return {name: env[name] for name in GATE_ENV_NAMES if (env.get(name) or "").strip()}


def write_gate_snapshot(bridge_dir: Path, snapshot: Mapping[str, str] | None) -> None:
    """Write (or with ``None`` remove) the hook's gate snapshot in *bridge_dir*.

    :param bridge_dir: Native Codex bridge directory the hook commands name.
    :param snapshot: Output of :func:`gate_env_snapshot`.
    """
    path = bridge_dir / _GATE_FILE
    if snapshot is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    _write_private_json(path, dict(snapshot))


def llmq_hooks_settings(bridge_dir: Path, python_executable: str | None) -> dict[str, object]:
    """The ``hooks.json`` payload registering the prompt gate and its settlement.

    :param bridge_dir: Native Codex bridge directory.
    :param python_executable: Python for the hook commands; ``None`` uses
        :data:`sys.executable`.
    :returns: A ``hooks.json``-shaped dict with ``UserPromptSubmit`` and ``Stop``.
    """

    def _hook(subcommand: str) -> dict[str, object]:
        import shlex

        return {
            "type": "command",
            "command": shlex.join(
                [
                    python_executable or sys.executable,
                    "-I",
                    "-m",
                    HOOK_MODULE,
                    subcommand,
                    "--bridge-dir",
                    str(bridge_dir),
                ]
            ),
            "timeout": HOOK_TIMEOUT_SECONDS,
        }

    return {
        "hooks": {
            "UserPromptSubmit": [{"hooks": [_hook("prompt-submit")]}],
            "Stop": [{"hooks": [_hook("stop")]}],
        }
    }


def mark_runner_turn(bridge_dir: Path) -> bool:
    """Mark the ``turn/start`` the runner's executor is about to issue.

    No-op unless this bridge has the prompt gate registered (its snapshot exists).

    :param bridge_dir: Native Codex bridge directory.
    :returns: Whether a marker was written.
    """
    if not (bridge_dir / _GATE_FILE).is_file():
        return False
    try:
        _write_private_json(bridge_dir / _RUNNER_TURN_FILE, {"at": time.time()})
    except OSError:
        return False
    return True


def clear_runner_turn(bridge_dir: Path) -> None:
    """Drop the marker of a ``turn/start`` that failed.

    :param bridge_dir: Native Codex bridge directory.
    """
    with contextlib.suppress(OSError):
        (bridge_dir / _RUNNER_TURN_FILE).unlink()


# ----------------------------------------------------------------- hook side


def load_gate_config(bridge_dir: Path, env: Mapping[str, str] | None = None) -> GateConfig | None:
    """The gate config for this hook, or ``None`` when the gate is inert.

    :param bridge_dir: Native Codex bridge directory.
    :param env: The hook's environment; ``None`` reads :data:`os.environ`.
    """
    source = _gate_source(bridge_dir, env)
    if source is None:
        return None
    from omnigent.runner.llmq_gate import GateConfig

    return GateConfig.from_env(source)


def _gate_source(
    bridge_dir: Path, env: Mapping[str, str] | None = None
) -> Mapping[str, str] | None:
    """What :meth:`GateConfig.from_env` is applied to, or ``None`` when surely inert.

    The launch-time snapshot when there is one, else the hook's own environment.
    A hook environment naming another quota route vetoes. Cheap: it imports
    neither llmq_gate nor httpx, so an inert hook answers in interpreter time.
    """
    hook_env: Mapping[str, str] = os.environ if env is None else env
    route = (hook_env.get(QUOTA_ROUTE_ENV_VAR) or "").strip()
    if route and route != PERSONAL_QUOTA_ROUTE:
        return None
    snapshot = _read_json(bridge_dir / _GATE_FILE)
    source: Mapping[str, str] = (
        {k: v for k, v in snapshot.items() if isinstance(v, str)}
        if snapshot is not None
        else hook_env
    )
    if (source.get(QUOTA_ROUTE_ENV_VAR) or "").strip() != PERSONAL_QUOTA_ROUTE or not all(
        (source.get(name) or "").strip() for name in _REQUIRED_ENV
    ):
        return None
    return source


class _Held(Exception):
    """llmq refused the prompt on fresh evidence: answer with a block."""

    def __init__(self, decision: GateDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class _Server:
    """The Omnigent server as the policy hook reaches it (``policy_hook.json``)."""

    def __init__(self, url: str, headers: dict[str, str], session_id: str) -> None:
        import httpx

        self._url = url
        self._headers = headers
        self._session_path = f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}"
        self.session_id = session_id
        self.client = httpx.AsyncClient(
            base_url=url, headers=headers, timeout=_SERVER_TIMEOUT_S, follow_redirects=False
        )

    @classmethod
    def from_bridge(cls, bridge_dir: Path, session_id: str) -> _Server | None:
        from omnigent.codex_native_bridge import read_policy_hook_config

        config = read_policy_hook_config(bridge_dir) or {}
        url = config.get("ap_server_url")
        if not isinstance(url, str) or not url:
            return None
        raw_headers = config.get("ap_auth_headers")
        headers = (
            {str(k): str(v) for k, v in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        return cls(url.rstrip("/"), headers, session_id)

    async def turbo(self) -> bool:
        """The session's current Turbo flag; off when it cannot be read."""
        import httpx

        from omnigent.native_policy_hook import (
            _is_login_redirect_or_unauthorized,
            policy_hook_reauth,
        )

        try:
            resp = await self.client.get(self._session_path)
            if _is_login_redirect_or_unauthorized(resp):
                # The one-shot launch token lapsed: re-mint once, like the
                # policy hook, and keep the fresh bearer for the status post.
                fresh = await asyncio.to_thread(policy_hook_reauth(self._url, self._headers))
                if fresh:
                    self.client.headers.update(fresh)
                    resp = await self.client.get(self._session_path)
            if resp.status_code == 200:
                body = resp.json()
                return isinstance(body, dict) and body.get("turbo") is True
        except (httpx.HTTPError, ValueError):
            pass
        return False

    async def post_gate(self, decision: GateDecision, turbo: bool) -> bool:
        """Post the ``quota_gate`` indicator; best-effort, never raises."""
        import httpx

        from omnigent._native_post_delivery import post_external_session_status

        try:
            await post_external_session_status(
                self.client,
                session_id=self.session_id,
                status="running",
                quota_gate=decision.status_payload(turbo=turbo),
            )
        except (httpx.HTTPError, RuntimeError, ValueError):
            return False
        return True


async def _prompt_submit(
    bridge_dir: Path,
    session_id: str,
    state: dict[str, object],
    payload: Mapping[str, object],
    config: GateConfig,
) -> dict[str, object]:
    import httpx

    from omnigent.runner.llmq_gate import (
        REFUSAL_REASONS,
        gate_codex_turn,
        normalize_model,
        unverified,
    )

    turn_id = _payload_str(payload, "turn_id")
    server = _Server.from_bridge(bridge_dir, session_id)
    turbo = False
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as llmq:
            if state.get("open"):
                # A new turn means the previous one ended without a Stop.
                await _settle_open(llmq, config, state)
                _save_state(bridge_dir, state)
            if _consume_runner_turn(bridge_dir):
                state["runner_turn_id"] = turn_id
                _save_state(bridge_dir, state)
                return {}  # the runner admitted this turn itself
            turbo = await server.turbo() if server is not None else False
            seen: list[GateDecision] = []

            async def read_turbo() -> bool:
                return turbo

            async def on_status(decision: GateDecision, _turbo: bool) -> None:
                seen.append(decision)

            async def sleep(seconds: float) -> None:
                if seen and not seen[-1].allow and seen[-1].reason in REFUSAL_REASONS:
                    raise _Held(seen[-1])
                await asyncio.sleep(seconds)

            model = normalize_model(_payload_str(payload, "model"), config.default_model)
            try:
                decision, key = await gate_codex_turn(
                    llmq,
                    config,
                    session_id=session_id,
                    turn_salt=f"tui:{turn_id or ''}:{time.time_ns()}",
                    model=model,
                    read_turbo=read_turbo,
                    on_status=on_status,
                    sleep=sleep,
                )
            except _Held as held:
                if server is not None and await server.post_gate(held.decision, turbo):
                    state["indicator"] = "blocked"
                    _save_state(bridge_dir, state)
                return {
                    "decision": "block",
                    "reason": held.decision.message(turbo=turbo) or held.decision.reason,
                }
            if decision.reservation_id is not None:
                state["open"] = {
                    "codex_turn_id": turn_id,
                    "turn_key": key,
                    "reservation_id": decision.reservation_id,
                }
                _save_state(bridge_dir, state)
                await _activate(llmq, config, decision, turn_key=key, model=model, turbo=turbo)
            if server is not None and (
                decision.state != "verified" or state.get("indicator", "verified") != "verified"
            ):
                # Show "unverified", or clear an earlier unverified/blocked chip.
                if await server.post_gate(decision, turbo):
                    state["indicator"] = decision.state
                    _save_state(bridge_dir, state)
            return {}
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the gate never blocks a prompt on its own faults
        if server is not None:
            with contextlib.suppress(Exception):
                await server.post_gate(unverified("gate_error"), turbo)
        return {}
    finally:
        if server is not None:
            with contextlib.suppress(Exception):
                await server.client.aclose()


async def _stop(
    bridge_dir: Path, state: dict[str, object], config: GateConfig
) -> dict[str, object]:
    import httpx

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as llmq:
        await _settle_open(llmq, config, state)
    _save_state(bridge_dir, state)
    return {}


async def _settle_open(
    llmq: httpx.AsyncClient, config: GateConfig, state: dict[str, object]
) -> str:
    """Settle the open reservation as a turn that RAN with unknown usage.

    Reuses the runner's :func:`close_codex_turn` with a tap that saw the turn
    dispatch and no usage: ``usage_unmeasured`` first, a cancel only when an
    older llmq answers 422, nothing on any other fault (the lease covers it).
    The reservation is forgotten either way, as the runner does.
    """
    from omnigent.runner.llmq_gate import GateDecision, TurnStreamTap, close_codex_turn

    raw = state.pop("open", None)
    if not isinstance(raw, dict):
        return "none"
    reservation_id = raw.get("reservation_id")
    key = raw.get("turn_key")
    if not isinstance(reservation_id, str) or not isinstance(key, str):
        return "none"
    tap = TurnStreamTap()
    tap.dispatched = True
    decision = GateDecision(
        allow=True, verified=True, reason="admitted", reservation_id=reservation_id
    )
    return await close_codex_turn(llmq, config, decision, turn_key=key, tap=tap)


async def _activate(
    llmq: httpx.AsyncClient,
    config: GateConfig,
    decision: GateDecision,
    *,
    turn_key: str,
    model: str,
    turbo: bool,
) -> bool:
    """Activate the admitted reservation before Codex dispatches (best effort).

    llmq_gate has no activation helper (the runner never activates), so this
    posts llmq's ``NativeTurnActivation`` directly: the admission body plus the
    reservation and a fresh activation id. Faults are ignored.
    """
    import httpx

    bearer = config.bearer()
    if bearer is None or decision.reservation_id is None:
        return False
    body = {
        "turn_key": turn_key,
        "agent_id": config.agent_id,
        "workstream": config.workstream,
        "harness": "codex",
        "model": model,
        "evidence": "controller",
        "turbo": turbo,
        "reservation_id": decision.reservation_id,
        "activation_id": "tui-" + uuid.uuid4().hex,
    }
    try:
        resp = await llmq.post(
            f"{config.controller_url}/v1/native-turns/activations",
            json=body,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=_REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


# ----------------------------------------------------------------- files


def _open_turn_id(state: Mapping[str, object]) -> object:
    raw = state.get("open")
    return raw.get("codex_turn_id") if isinstance(raw, dict) else None


def _load_state(bridge_dir: Path, session_id: str) -> dict[str, object]:
    state = _read_json(bridge_dir / _STATE_FILE)
    if state is None or state.get("session_id") != session_id:
        # A /clear rotation or fork reuses the bridge dir: start over.
        return {"session_id": session_id}
    return state


def _save_state(bridge_dir: Path, state: Mapping[str, object]) -> None:
    with contextlib.suppress(OSError):
        _write_private_json(bridge_dir / _STATE_FILE, dict(state))


def _consume_runner_turn(bridge_dir: Path) -> bool:
    """Take the runner's ``turn/start`` marker, if a fresh one is waiting."""
    marker = bridge_dir / _RUNNER_TURN_FILE
    taken = marker.with_name(f"{_RUNNER_TURN_FILE}.{os.getpid()}")
    try:
        os.replace(marker, taken)
    except OSError:
        return False
    data = _read_json(taken)
    with contextlib.suppress(OSError):
        taken.unlink()
    at = data.get("at") if data is not None else None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return False
    return 0 <= time.time() - at <= RUNNER_TURN_TTL_SECONDS


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _payload_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


# ----------------------------------------------------------------- entrypoint


def main(argv: list[str] | None = None) -> int:
    """Run one hook invocation; always exit 0 with ``{}`` or a block on stdout.

    :param argv: ``["prompt-submit" | "stop", "--bridge-dir", <dir>]``;
        ``None`` reads :data:`sys.argv`.
    :returns: ``0``.
    """
    # Codex parses stdout as the verdict, and stderr is not ours to fill: the
    # gate's own log lines (llmq_gate warns on every fail-open) and anything an
    # imported module prints are discarded, whatever logging config is active.
    stdout = sys.stdout
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    out: dict[str, object] = {}
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            out = _run(sys.argv[1:] if argv is None else argv)
    except Exception:  # noqa: BLE001 - fail open on anything
        out = {}
    finally:
        logging.disable(previous)
    stdout.write(json.dumps(out))
    stdout.flush()
    return 0


def _run(argv: list[str]) -> dict[str, object]:
    parser = argparse.ArgumentParser(prog=f"python -m {HOOK_MODULE}", add_help=False)
    parser.add_argument("event", choices=("prompt-submit", "stop"))
    parser.add_argument("--bridge-dir", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return {}
    bridge_dir = Path(args.bridge_dir)
    source = _gate_source(bridge_dir)
    if source is None:
        return {}
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    from omnigent.codex_native_bridge import read_bridge_state

    bridge = read_bridge_state(bridge_dir)
    if bridge is None:
        return {}
    state = _load_state(bridge_dir, bridge.session_id)
    # The light exits: no reservation to settle at Stop, a steer into a turn
    # already gated, or a turn the runner delivered (and gated) itself. None of
    # them imports llmq_gate or httpx.
    if args.event == "stop":
        if not state.get("open"):
            return {}
    else:
        turn_id = _payload_str(payload, "turn_id")
        if turn_id is not None and turn_id in {state.get("runner_turn_id"), _open_turn_id(state)}:
            return {}
        if not state.get("open") and _consume_runner_turn(bridge_dir):
            state["runner_turn_id"] = turn_id
            _save_state(bridge_dir, state)
            return {}
    from omnigent.runner.llmq_gate import GateConfig

    config = GateConfig.from_env(source)
    if config is None:
        return {}
    if args.event == "stop":
        work = _stop(bridge_dir, state, config)
    else:
        work = _prompt_submit(bridge_dir, bridge.session_id, state, payload, config)
    try:
        return asyncio.run(asyncio.wait_for(work, timeout=_HOOK_BUDGET_S))
    except (TimeoutError, asyncio.TimeoutError):
        return {}


if __name__ == "__main__":
    raise SystemExit(main())
