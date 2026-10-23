"""The terminal-UI Codex llmq hook (design #76 "B (TUI hook)", phase 3b).

Two halves:

* registration: the generated ``CODEX_HOME/hooks.json`` carries the hook
  (``UserPromptSubmit`` + ``Stop``) only for terminal-UI sessions whose launch
  provisions the gate, the executor marks the turns the runner delivers, and the
  trust pass covers the hook;
* the hook itself, driven through :func:`omnigent.codex_llmq_hook.main` the way
  Codex runs it (stdin payload, stdout verdict) against a scripted llmq and
  Omnigent server: block on a fresh refusal, ``{}`` on admission, fail open on every
  fault, inert without the gate env, settle unmeasured at ``Stop``, and never a
  secret or a path in the output.
"""

from __future__ import annotations

import io
import json
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import codex_llmq_hook
from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
    write_policy_hook_config,
)

SESSION = "conv_tui_1"
LLMQ_TOKEN = "llmq-sekrit-token-0123456789"
SERVER_TOKEN = "omni-sekrit-bearer-9876543210"
ACTOR = "fleet-test"
RESETS_AT = 1_900_000_000.0


def _gate_env(token_file: Path, url: str = "http://llmq.test") -> dict[str, str]:
    return {
        "OMNIGENT_QUOTA_ROUTE": "personal-llmq",
        "LLMQ_CONTROLLER_URL": url,
        "LLMQ_TOKEN_FILE": str(token_file),
        "LLMQ_AGENT_ID": ACTOR,
    }


def _clean_gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in codex_llmq_hook.GATE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# ===================================================================== hook


def _admitted(**extra: Any) -> dict[str, Any]:
    return {
        "allowed": True,
        "reason": "admitted",
        "verified": True,
        "reservation_id": "res-tui-00000001",
        "credit_bearing": False,
        **extra,
    }


MARGIN = {
    "allowed": False,
    "reason": "credit_safety_margin",
    "verified": True,
    "resets_at": RESETS_AT,
}


class World:
    """A bridge dir plus a scripted llmq and Omnigent server, wired as Codex would."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.bridge = tmp_path / "bridge-dir"
        self.token_file = tmp_path / "secret-dir" / "llmq-token"
        self.token_file.parent.mkdir()
        self.token_file.write_text(LLMQ_TOKEN + "\n")
        write_bridge_state(
            self.bridge,
            CodexNativeBridgeState(
                session_id=SESSION,
                socket_path="ws://127.0.0.1:1",
                thread_id="thread_1",
                codex_home=str(self.bridge / "codex-home"),
                cwd=str(tmp_path),
            ),
        )
        write_policy_hook_config(
            self.bridge,
            ap_server_url="http://omni.test",
            ap_auth_headers={"Authorization": f"Bearer {SERVER_TOKEN}"},
        )
        codex_llmq_hook.write_gate_snapshot(self.bridge, _gate_env(self.token_file))
        _clean_gate_env(monkeypatch)
        self.turbo = False
        self.admission: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            200, json=_admitted()
        )
        self.settlement: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            200, json={"status": "settled"}
        )
        self.server_up = True
        self.calls: list[tuple[str, str, Any]] = []
        transport = httpx.MockTransport(self._handle)
        real = self.real_client = httpx.AsyncClient

        def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _client)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.host, request.url.path, body))
        if request.url.host == "omni.test":
            if not self.server_up:
                raise httpx.ConnectError("server down", request=request)
            assert request.headers["authorization"] == f"Bearer {SERVER_TOKEN}"
            if request.method == "GET":
                return httpx.Response(200, json={"id": SESSION, "turbo": self.turbo})
            return httpx.Response(200, json={"queued": False})
        assert request.headers["authorization"] == f"Bearer {LLMQ_TOKEN}"
        path = request.url.path
        if path.endswith("/admissions"):
            return self.admission(request)
        if path.endswith("/activations"):
            return httpx.Response(200, json={"reservation_id": body["reservation_id"]})
        if path.endswith("/settlements"):
            return self.settlement(request)
        if path.endswith("/cancellations"):
            return httpx.Response(200, json={"status": "cancelled"})
        raise AssertionError(f"unexpected llmq call {path}")

    def run(self, event: str = "prompt-submit", payload: dict[str, Any] | None = None) -> str:
        payload = (
            {
                "hook_event_name": "UserPromptSubmit" if event == "prompt-submit" else "Stop",
                "session_id": "codex-thread-1",
                "turn_id": "turn-1",
                "model": "gpt-6.1-sol",
                "prompt": "do the thing",
                "cwd": "/somewhere",
                "transcript_path": "/somewhere/rollout.jsonl",
                "permission_mode": "default",
            }
            if payload is None
            else payload
        )
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        self.monkeypatch.setattr(sys, "stdout", out)
        assert codex_llmq_hook.main([event, "--bridge-dir", str(self.bridge)]) == 0
        return out.getvalue()

    def llmq_paths(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for host, path, _ in self.calls if host == "llmq.test"]

    def gates(self) -> list[dict[str, Any]]:
        """The ``quota_gate`` objects posted to the server, in order."""
        return [
            body["data"]["quota_gate"]
            for host, path, body in self.calls
            if host == "omni.test" and path.endswith("/events")
        ]

    def state(self) -> dict[str, Any]:
        return json.loads((self.bridge / "llmq_gate_state.json").read_text())


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def _assert_clean(out: str, world: World) -> None:
    """No secret, token, URL or file path ever reaches Codex."""
    for needle in (
        LLMQ_TOKEN,
        SERVER_TOKEN,
        str(world.bridge),
        str(world.token_file),
        "llmq.test",
        "omni.test",
        "/",
    ):
        assert needle not in out, needle


def test_fresh_margin_refusal_blocks_with_resets_at_and_turbo_hint(world: World) -> None:
    world.admission = lambda r: httpx.Response(200, json=MARGIN)
    out = world.run()
    verdict = json.loads(out)
    assert verdict["decision"] == "block"
    reason = verdict["reason"]
    assert "credit-safety margin (90%)" in reason
    assert time.strftime("resets at %H:%M UTC", time.gmtime(RESETS_AT)) in reason
    assert "Enable Turbo on this session" in reason
    assert world.llmq_paths() == ["admissions"]  # one ask, no hold loop, no reservation
    assert [g["state"] for g in world.gates()] == ["blocked"]
    assert world.gates()[0]["resets_at"] == RESETS_AT
    _assert_clean(out, world)


def test_admission_answers_empty_object_and_records_an_activated_reservation(
    world: World,
) -> None:
    out = world.run()
    assert json.loads(out) == {}
    assert world.llmq_paths() == ["admissions", "activations"]
    admission = next(b for _h, p, b in world.calls if p.endswith("/admissions"))
    assert admission["evidence"] == "controller" and admission["turbo"] is False
    assert admission["model"] == "gpt-6.1-sol" and admission["agent_id"] == ACTOR
    activation = next(b for _h, p, b in world.calls if p.endswith("/activations"))
    assert activation["reservation_id"] == "res-tui-00000001"
    assert activation["turn_key"] == admission["turn_key"]
    assert world.state()["open"]["reservation_id"] == "res-tui-00000001"
    assert world.gates() == []  # verified, nothing to show or clear


def test_every_prompt_gets_a_fresh_turn_key(world: World) -> None:
    world.run()
    world.run(payload={"hook_event_name": "UserPromptSubmit", "turn_id": "turn-2"})
    keys = [b["turn_key"] for _h, p, b in world.calls if p.endswith("/admissions")]
    assert len(keys) == 2 and len(set(keys)) == 2


def test_turbo_is_read_from_the_server_and_sent_to_llmq(world: World) -> None:
    world.turbo = True
    world.admission = lambda r: httpx.Response(200, json=_admitted(credit_bearing=True))
    assert json.loads(world.run()) == {}
    admission = next(b for _h, p, b in world.calls if p.endswith("/admissions"))
    assert admission["turbo"] is True
    activation = next(b for _h, p, b in world.calls if p.endswith("/activations"))
    assert activation["turbo"] is True


def _raise(exc: type[httpx.HTTPError]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("fault", request=request)

    return handler


@pytest.mark.parametrize(
    ("admission", "slug"),
    [
        (_raise(httpx.ConnectError), "llmq_unreachable"),
        (_raise(httpx.ReadTimeout), "llmq_unreachable"),
        (lambda r: httpx.Response(500, text="boom"), "llmq_http_500"),
        (lambda r: httpx.Response(503, text="down"), "llmq_http_503"),
        (lambda r: httpx.Response(403, json={"detail": "identity"}), "llmq_http_403"),
        (lambda r: httpx.Response(422, json={"detail": []}), "llmq_http_422"),
        (lambda r: httpx.Response(200, text="<html>"), "llmq_invalid_response"),
        (lambda r: httpx.Response(200, json=["not", "a", "decision"]), "llmq_unknown_shape"),
        (lambda r: httpx.Response(200, json={"allowed": "yes"}), "llmq_unknown_shape"),
        (
            lambda r: httpx.Response(
                200,
                json={
                    "allowed": True,
                    "reason": "x",
                    "verified": False,
                    "unverified_reason": "receipt_stale",
                },
            ),
            "receipt_stale",
        ),
    ],
)
def test_every_llmq_fault_fails_open_with_the_unverified_indicator(
    world: World, admission: Callable[[httpx.Request], httpx.Response], slug: str
) -> None:
    world.admission = admission
    out = world.run()
    assert json.loads(out) == {}
    assert [(g["state"], g["reason"]) for g in world.gates()] == [("unverified", slug)]
    assert "activations" not in world.llmq_paths()
    assert "open" not in world.state()
    _assert_clean(out, world)


def test_missing_token_file_fails_open_without_calling_llmq(world: World) -> None:
    world.token_file.unlink()
    out = world.run()
    assert json.loads(out) == {}
    assert world.llmq_paths() == []
    assert [(g["state"], g["reason"]) for g in world.gates()] == [
        ("unverified", "token_unavailable")
    ]


def test_unreachable_server_still_admits_with_turbo_off(world: World) -> None:
    world.server_up = False
    world.admission = lambda r: httpx.Response(200, json=MARGIN)
    out = world.run()
    verdict = json.loads(out)
    assert verdict["decision"] == "block"  # Turbo unreadable -> off, the default
    admission = next(b for _h, p, b in world.calls if p.endswith("/admissions"))
    assert admission["turbo"] is False
    _assert_clean(out, world)


def test_missing_server_coordinates_fail_open_without_indicator(world: World) -> None:
    (world.bridge / "policy_hook.json").unlink()
    world.admission = _raise(httpx.ConnectError)
    assert json.loads(world.run()) == {}
    assert world.gates() == []


def test_missing_bridge_state_is_a_silent_allow(world: World) -> None:
    (world.bridge / "state.json").unlink()
    assert json.loads(world.run()) == {}
    assert world.calls == []


@pytest.mark.parametrize("payload", ["not json", "[1, 2]", ""])
def test_malformed_payload_still_gates(world: World, payload: str, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    codex_llmq_hook.main(["prompt-submit", "--bridge-dir", str(world.bridge)])
    assert json.loads(out.getvalue()) == {}
    assert world.llmq_paths()[:1] == ["admissions"]


def test_unexpected_fault_fails_open_with_gate_error(world: World, monkeypatch) -> None:
    def explode(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr("omnigent.runner.llmq_gate.gate_codex_turn", explode)
    assert json.loads(world.run()) == {}
    assert [(g["state"], g["reason"]) for g in world.gates()] == [("unverified", "gate_error")]


def test_bad_arguments_answer_empty_and_print_nothing_else(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert codex_llmq_hook.main(["bogus"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "{}" and captured.err == ""


def test_output_and_stderr_carry_no_secrets_or_paths(world: World, capsys, monkeypatch) -> None:
    for admission in (
        lambda r: httpx.Response(200, json=MARGIN),
        _raise(httpx.ConnectError),
        lambda r: httpx.Response(500, text=f"token {LLMQ_TOKEN} at {world.token_file}"),
    ):
        world.admission = admission
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"turn_id": "t"})))
        codex_llmq_hook.main(["prompt-submit", "--bridge-dir", str(world.bridge)])
        captured = capsys.readouterr()
        _assert_clean(captured.out, world)
        assert captured.err == ""


# ----------------------------------------------------------------- inert


def test_inert_without_the_gate_env_makes_no_network_call(world: World) -> None:
    codex_llmq_hook.write_gate_snapshot(world.bridge, None)
    started = time.monotonic()
    assert json.loads(world.run()) == {}
    assert world.calls == []
    assert time.monotonic() - started < 1.0


def test_inert_hook_never_imports_llmq_gate(tmp_path: Path) -> None:
    """The inert path answers in interpreter time: no llmq_gate, no httpx."""
    import subprocess

    probe = (
        "import sys, io;"
        "sys.stdin = io.StringIO('{}');"
        "from omnigent import codex_llmq_hook;"
        f"codex_llmq_hook.main(['prompt-submit', '--bridge-dir', {str(tmp_path)!r}]);"
        "sys.stdout.write('|' + str(sorted(m for m in ('httpx', 'omnigent.runner.llmq_gate')"
        " if m in sys.modules)))"
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, env=env, check=True
    )
    assert done.stdout == "{}|[]"
    assert done.stderr == ""


@pytest.mark.parametrize(
    ("drop", "route"),
    [
        ("LLMQ_CONTROLLER_URL", None),
        ("LLMQ_TOKEN_FILE", None),
        ("LLMQ_AGENT_ID", None),
        ("OMNIGENT_QUOTA_ROUTE", None),
        (None, "work-vertex"),
    ],
)
def test_snapshot_must_satisfy_the_runner_gate_rule(
    world: World, drop: str | None, route: str | None
) -> None:
    env = _gate_env(world.token_file)
    if drop:
        env.pop(drop)
    if route:
        env["OMNIGENT_QUOTA_ROUTE"] = route
    (world.bridge / "llmq_gate.json").write_text(json.dumps(env))
    assert json.loads(world.run()) == {}
    assert world.calls == []


def test_a_hook_env_naming_another_route_vetoes(world: World, monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_QUOTA_ROUTE", "work-vertex")
    assert json.loads(world.run()) == {}
    assert world.calls == []


def test_without_a_snapshot_the_hook_env_is_used_with_the_same_rule(
    world: World, monkeypatch
) -> None:
    codex_llmq_hook.write_gate_snapshot(world.bridge, None)
    for name, value in _gate_env(world.token_file).items():
        monkeypatch.setenv(name, value)
    assert json.loads(world.run()) == {}
    assert world.llmq_paths()[:1] == ["admissions"]


# ----------------------------------------------------------------- runner turns


def test_a_runner_delivered_turn_is_not_admitted_twice(world: World) -> None:
    assert codex_llmq_hook.mark_runner_turn(world.bridge) is True
    assert json.loads(world.run()) == {}
    assert world.calls == []
    assert not (world.bridge / "llmq_runner_turn.json").exists()
    # A steer into that same runner turn is not a new turn either.
    assert json.loads(world.run()) == {}
    assert world.calls == []
    # The next typed prompt (a new turn) is gated again.
    world.run(payload={"hook_event_name": "UserPromptSubmit", "turn_id": "turn-2"})
    assert world.llmq_paths()[:1] == ["admissions"]


def test_a_stale_runner_marker_does_not_skip_a_typed_prompt(world: World) -> None:
    codex_llmq_hook.mark_runner_turn(world.bridge)
    marker = world.bridge / "llmq_runner_turn.json"
    marker.write_text(json.dumps({"at": time.time() - 3600}))
    world.run()
    assert world.llmq_paths()[:1] == ["admissions"]


def test_runner_marker_is_only_written_where_the_gate_is_registered(tmp_path: Path) -> None:
    assert codex_llmq_hook.mark_runner_turn(tmp_path) is False
    assert not (tmp_path / "llmq_runner_turn.json").exists()


def test_a_steer_into_an_admitted_turn_is_not_admitted_again(world: World) -> None:
    world.run()
    world.run()  # same turn_id
    assert world.llmq_paths() == ["admissions", "activations"]


# ----------------------------------------------------------------- settlement


def test_stop_settles_the_turn_unmeasured_never_with_zero_usage(world: World) -> None:
    world.run()
    out = world.run("stop", {"hook_event_name": "Stop", "turn_id": "turn-1"})
    assert json.loads(out) == {}
    settle = [b for _h, p, b in world.calls if p.endswith("/settlements")]
    assert settle == [
        {
            "turn_key": settle[0]["turn_key"],
            "reservation_id": "res-tui-00000001",
            "usage_unmeasured": True,
        }
    ]
    assert "usage" not in settle[0]
    assert "cancellations" not in world.llmq_paths()
    assert "open" not in world.state()


def test_stop_falls_back_to_cancel_only_when_llmq_rejects_the_body(world: World) -> None:
    world.settlement = lambda r: httpx.Response(422, json={"detail": "extra field"})
    world.run()
    world.run("stop", {"hook_event_name": "Stop", "turn_id": "turn-1"})
    assert world.llmq_paths()[-2:] == ["settlements", "cancellations"]


@pytest.mark.parametrize(
    "settlement",
    [
        lambda r: httpx.Response(500, text="boom"),
        lambda r: httpx.Response(409, json={"detail": "conflict"}),
        _raise(httpx.ConnectError),
    ],
)
def test_stop_never_cancels_on_any_other_fault(
    world: World, settlement: Callable[[httpx.Request], httpx.Response]
) -> None:
    world.settlement = settlement
    world.run()
    assert json.loads(world.run("stop", {"hook_event_name": "Stop"})) == {}
    assert "cancellations" not in world.llmq_paths()
    assert "open" not in world.state()  # forgotten, as the runner does: the lease covers it


def test_stop_without_a_reservation_makes_no_call(world: World) -> None:
    assert json.loads(world.run("stop", {"hook_event_name": "Stop"})) == {}
    assert world.calls == []


def test_next_prompt_settles_a_turn_whose_stop_never_fired(world: World) -> None:
    world.run()
    world.run(payload={"hook_event_name": "UserPromptSubmit", "turn_id": "turn-2"})
    assert world.llmq_paths() == [
        "admissions",
        "activations",
        "settlements",
        "admissions",
        "activations",
    ]
    settle = next(b for _h, p, b in world.calls if p.endswith("/settlements"))
    assert settle["usage_unmeasured"] is True


# ----------------------------------------------------------------- indicator


def test_a_verified_admission_clears_an_earlier_indicator(world: World) -> None:
    world.admission = _raise(httpx.ConnectError)
    world.run()
    world.admission = lambda r: httpx.Response(200, json=_admitted())
    world.run(payload={"hook_event_name": "UserPromptSubmit", "turn_id": "turn-2"})
    world.run(payload={"hook_event_name": "UserPromptSubmit", "turn_id": "turn-3"})
    assert [g["state"] for g in world.gates()] == ["unverified", "verified"]


# ----------------------------------------------------------------- 5 s, no retry


def test_hung_controller_fails_open_after_five_seconds_without_retry(world: World) -> None:
    """The hook's own clock: one connection, ~5 s, then ``{}`` with the indicator."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    connections: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            connections.append(conn)  # never answered

    threading.Thread(target=accept, daemon=True).start()
    port = listener.getsockname()[1]
    (world.bridge / "llmq_gate.json").write_text(
        json.dumps(_gate_env(world.token_file, url=f"http://127.0.0.1:{port}"))
    )
    write_policy_hook_config(world.bridge, ap_server_url="http://127.0.0.1:1", ap_auth_headers={})
    world.monkeypatch.setattr(httpx, "AsyncClient", world.real_client)
    try:
        started = time.monotonic()
        out = world.run()
        elapsed = time.monotonic() - started
    finally:
        listener.close()
        for conn in connections:
            conn.close()
    assert json.loads(out) == {}
    assert 4.5 <= elapsed <= 9.0, elapsed
    assert len(connections) == 1


# ===================================================================== registration


def _hook_commands(payload: dict[str, Any], event: str) -> list[str]:
    return [
        hook["command"] for group in payload["hooks"].get(event, []) for hook in group["hooks"]
    ]


def test_writer_registers_the_prompt_gate_and_its_stop_settlement(tmp_path: Path) -> None:
    from omnigent.codex_native_app_server import _write_codex_policy_hooks_file

    codex_home = tmp_path / "codex-home"
    bridge = tmp_path / "bridge"
    _write_codex_policy_hooks_file(codex_home, bridge, "/venv/bin/python", llmq_gate=True)
    payload = json.loads((codex_home / "hooks.json").read_text())
    prompt = _hook_commands(payload, "UserPromptSubmit")
    assert "omnigent.codex_native_hook evaluate-policy" in prompt[0]
    assert prompt[-1] == (
        f"/venv/bin/python -I -m omnigent.codex_llmq_hook prompt-submit --bridge-dir {bridge}"
    )
    assert _hook_commands(payload, "Stop") == [
        f"/venv/bin/python -I -m omnigent.codex_llmq_hook stop --bridge-dir {bridge}"
    ]
    timeouts = {
        hook["timeout"]
        for event in ("UserPromptSubmit", "Stop")
        for group in payload["hooks"][event]
        for hook in group["hooks"]
        if "codex_llmq_hook" in hook["command"]
    }
    assert timeouts == {codex_llmq_hook.HOOK_TIMEOUT_SECONDS}


def test_writer_registers_no_llmq_hook_by_default(tmp_path: Path) -> None:
    from omnigent.codex_native_app_server import _write_codex_policy_hooks_file

    codex_home = tmp_path / "codex-home"
    _write_codex_policy_hooks_file(codex_home, tmp_path / "bridge", "/venv/bin/python")
    text = (codex_home / "hooks.json").read_text()
    assert "codex_llmq_hook" not in text
    assert "Stop" not in json.loads(text)["hooks"]


async def _no_wait(self: Any) -> None:
    return None


def _app_server(tmp_path: Path, *, llmq_prompt_gate: bool) -> Any:
    from omnigent.codex_native_app_server import CodexNativeAppServer

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        env={"HARNESS_CODEX_NATIVE_REQUEST_SESSION_ID": "f" * 32},
        config_overrides=[],
        cwd=workspace,
        bridge_dir=tmp_path / "bridge",
        request_session_id="f" * 32,
        python_executable="/new/python",
        llmq_prompt_gate=llmq_prompt_gate,
    )


@pytest.fixture
def startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from omnigent.codex_native_app_server import CodexNativeAppServer

    real_home = tmp_path / "real-codex-home"
    real_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_home))
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _no_wait)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _no_wait)

    async def _version(_path: str) -> tuple[int, int, int]:
        return (0, 159, 1)

    monkeypatch.setattr("omnigent.codex_native_app_server._codex_cli_version", _version)
    _clean_gate_env(monkeypatch)
    token = tmp_path / "token-dir" / "token"
    token.parent.mkdir()
    token.write_text(LLMQ_TOKEN)
    return token


@pytest.mark.parametrize(
    ("terminal_ui", "provisioned", "registered"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
async def test_start_registers_the_hook_only_for_provisioned_terminal_ui_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    startup: Path,
    terminal_ui: bool,
    provisioned: bool,
    registered: bool,
) -> None:
    if provisioned:
        for name, value in _gate_env(startup).items():
            monkeypatch.setenv(name, value)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "llmq_gate.json").write_text("{}")  # stale snapshot from an earlier launch
    server = _app_server(tmp_path, llmq_prompt_gate=terminal_ui)
    await server.start()
    try:
        hooks = json.loads((tmp_path / "codex-home" / "hooks.json").read_text())
        commands = _hook_commands(hooks, "UserPromptSubmit") + _hook_commands(hooks, "Stop")
        assert any("codex_llmq_hook" in c for c in commands) is registered
        assert server.llmq_hooks_registered is registered
        snapshot = bridge / "llmq_gate.json"
        assert snapshot.exists() is registered
        if registered:
            saved = json.loads(snapshot.read_text())
            assert saved == _gate_env(startup)  # the token FILE path, never the token
            assert LLMQ_TOKEN not in snapshot.read_text()
            assert snapshot.stat().st_mode & 0o077 == 0
    finally:
        await server.close()


def test_build_codex_native_server_carries_the_terminal_ui_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.codex_native_app_server import build_codex_native_server

    common = {
        "socket_path": tmp_path / "s.sock",
        "codex_home": tmp_path / "home",
        "cwd": tmp_path,
        "model": None,
        "profile": None,
        "bridge_dir": tmp_path / "bridge",
        "request_session_id": "f" * 32,
        "codex_path": sys.executable,
    }
    assert build_codex_native_server(**common).llmq_prompt_gate is False
    assert build_codex_native_server(**common, llmq_prompt_gate=True).llmq_prompt_gate is True


class _SnapshotClient:
    def __init__(self, labels: dict[str, str] | None) -> None:
        self._labels = labels

    async def get(self, url: str, timeout: float | None = None) -> Any:
        payload = {"labels": self._labels} if self._labels is not None else {}

        class _R:
            status_code = 200

            @staticmethod
            def json() -> Any:
                return payload

        return _R()


@pytest.mark.parametrize(
    ("labels", "terminal_ui"),
    [
        ({"omnigent.wrapper": "codex-native-ui", "omnigent.ui": "terminal"}, True),
        ({"omnigent.wrapper": "codex-native-ui-subagent"}, False),
        ({"omnigent.wrapper": "claude-code-native-ui"}, False),
        ({}, False),
        (None, False),
    ],
)
async def test_runner_launch_config_knows_the_terminal_ui_wrapper(
    monkeypatch: pytest.MonkeyPatch, labels: dict[str, str] | None, terminal_ui: bool
) -> None:
    from omnigent.runner.native.orchestration import _codex_native_launch_config

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    config = await _codex_native_launch_config(
        session_id="conv_1",
        server_client=_SnapshotClient(labels),  # type: ignore[arg-type]
    )
    assert config.terminal_ui is terminal_ui


async def test_llmq_hooks_are_trusted_without_touching_other_hooks() -> None:
    from omnigent.codex_native_app_server import trust_codex_llmq_hooks

    hooks = [
        {
            "key": "gate",
            "command": "/py -I -m omnigent.codex_llmq_hook prompt-submit --bridge-dir /b",
            "trustStatus": "untrusted",
            "currentHash": "sha256:g",
        },
        {
            "key": "stop",
            "command": "/py -I -m omnigent.codex_llmq_hook stop --bridge-dir /b",
            "trustStatus": "untrusted",
            "currentHash": "sha256:s",
        },
        {
            "key": "policy",
            "command": "/py -m omnigent.codex_native_hook evaluate-policy --bridge-dir /b",
            "trustStatus": "untrusted",
            "currentHash": "sha256:p",
        },
        {
            "key": "user",
            "command": "bash guard.sh",
            "trustStatus": "untrusted",
            "currentHash": "sha256:u",
        },
    ]
    writes: list[dict[str, Any]] = []

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "config/batchWrite":
            value = params["edits"][0]["value"]
            writes.append(value)
            for hook in hooks:
                if hook["key"] in value:
                    hook["trustStatus"] = "trusted"
            return {"result": {}}
        return {"result": {"data": [{"cwd": "/w", "hooks": [dict(h) for h in hooks]}]}}

    assert await trust_codex_llmq_hooks(request, cwd="/w") == []
    assert writes == [{"gate": {"trusted_hash": "sha256:g"}, "stop": {"trusted_hash": "sha256:s"}}]


async def test_start_trust_step_covers_the_llmq_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.codex_native_app_server import CodexAppServerClient

    trusted: list[str] = []

    async def _noop(self: Any) -> None:
        return None

    async def _policy(client: Any, *, cwd: str) -> None:
        trusted.append("policy")

    async def _llmq(request: Any, *, cwd: str) -> list[str]:
        trusted.append("llmq")
        return []

    monkeypatch.setattr(CodexAppServerClient, "connect", _noop)
    monkeypatch.setattr(CodexAppServerClient, "close", _noop)
    monkeypatch.setattr("omnigent.codex_native_app_server.trust_native_policy_hooks", _policy)
    monkeypatch.setattr("omnigent.codex_native_app_server.trust_codex_llmq_hooks", _llmq)
    server = _app_server(tmp_path, llmq_prompt_gate=True)
    server.llmq_hooks_registered = True
    await server._trust_policy_hooks()
    assert trusted == ["policy", "llmq"]
    trusted.clear()
    server.llmq_hooks_registered = False
    await server._trust_policy_hooks()
    assert trusted == ["policy"]


@pytest.mark.parametrize("fails", [False, True])
async def test_executor_marks_the_turn_start_it_issues(tmp_path: Path, fails: bool) -> None:
    from omnigent.inner.codex_native_executor import _start_codex_turn

    codex_llmq_hook.write_gate_snapshot(tmp_path, {"OMNIGENT_QUOTA_ROUTE": "personal-llmq"})
    marker = tmp_path / "llmq_runner_turn.json"
    seen: list[bool] = []

    class _Client:
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            seen.append(marker.exists())
            if fails:
                raise RuntimeError("busy")
            return {"result": {"turn": {"id": "turn_9"}}}

    state = CodexNativeBridgeState(
        session_id=SESSION,
        socket_path="ws://127.0.0.1:1",
        thread_id="thread_1",
        codex_home=str(tmp_path / "codex-home"),
        cwd=str(tmp_path),
    )
    write_bridge_state(tmp_path, state)
    if fails:
        with pytest.raises(RuntimeError):
            await _start_codex_turn(
                _Client(),  # type: ignore[arg-type]
                bridge_dir=tmp_path,
                state=state,
                input_items=[{"type": "text", "text": "hi"}],
                settings_overrides={},
            )
    else:
        await _start_codex_turn(
            _Client(),  # type: ignore[arg-type]
            bridge_dir=tmp_path,
            state=state,
            input_items=[{"type": "text", "text": "hi"}],
            settings_overrides={},
        )
    assert seen == [True]  # marked before turn/start went out
    assert marker.exists() is not fails  # a failed turn/start leaves no marker
