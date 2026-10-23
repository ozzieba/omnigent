"""The terminal-UI Codex llmq hook against llmq's real app (contract test).

Drives :func:`omnigent.codex_llmq_hook.main` exactly as Codex runs it (stdin
payload in, verdict on stdout) with its llmq calls served by chatgpt-playground's
FastAPI app (``llm_quota_controller.api.create_app``) through
``httpx.ASGITransport``, and the Omnigent server (Turbo read, indicator post)
scripted. Reuses the llmq harness of the runner gate's contract test, so the
receipt fixture and settings are the ones that test pins.

Needs <chatgpt-playground>/llm-quota-controller/src on PYTHONPATH; skipped
otherwise. Passes against llmq main and against #1328: with #1328 the ``Stop``
settlement is llmq's retained estimate; without it llmq rejects the
``usage_unmeasured`` body (422), the hook falls back to a cancel, and llmq refuses
that cancel for a turn it activated (409), so the reservation is left to its
lease, never cancelled as "never ran".
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("llm_quota_controller.api")

from omnigent import codex_llmq_hook
from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
    write_policy_hook_config,
)
from tests.runner.test_llmq_gate_contract_llmq_main import (
    ACTOR,
    UNMEASURED_FORM,
    Llmq,
)

SESSION = "conv_tui_contract"


class Tui:
    """One terminal-UI session's bridge dir, wired to llmq's real app."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llmq: Llmq) -> None:
        self.monkeypatch = monkeypatch
        self.llmq = llmq
        self.bridge = tmp_path / "bridge"
        token = tmp_path / "token"
        token.write_text("agent-token\n")
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
        write_policy_hook_config(self.bridge, ap_server_url="http://omni.test", ap_auth_headers={})
        codex_llmq_hook.write_gate_snapshot(
            self.bridge,
            {
                "OMNIGENT_QUOTA_ROUTE": "personal-llmq",
                "LLMQ_CONTROLLER_URL": "http://llmq.test",
                "LLMQ_TOKEN_FILE": str(token),
                "LLMQ_AGENT_ID": ACTOR,
            },
        )
        for name in codex_llmq_hook.GATE_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        self.turbo = False
        self.llmq_calls: list[tuple[str, int, Any]] = []
        self.gates: list[dict[str, Any]] = []
        outer = self
        asgi = httpx.ASGITransport(app=llmq.app)

        class Route(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                if request.url.host == "omni.test":
                    if request.method == "GET":
                        return httpx.Response(200, json={"id": SESSION, "turbo": outer.turbo})
                    outer.gates.append(json.loads(request.content)["data"]["quota_gate"])
                    return httpx.Response(200, json={"queued": False})
                response = await asgi.handle_async_request(request)
                body = await response.aread()
                outer.llmq_calls.append(
                    (
                        request.url.path.rsplit("/", 1)[-1],
                        response.status_code,
                        json.loads(request.content),
                    )
                )
                return httpx.Response(response.status_code, headers=response.headers, content=body)

        real = httpx.AsyncClient

        def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = Route()
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _client)

    def run(self, event: str = "prompt-submit", turn_id: str = "turn-1") -> dict[str, Any]:
        payload = {
            "hook_event_name": "UserPromptSubmit" if event == "prompt-submit" else "Stop",
            "turn_id": turn_id,
            "model": "gpt-6.1-sol",
            "prompt": "hello",
        }
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        out = io.StringIO()
        self.monkeypatch.setattr(sys, "stdout", out)
        codex_llmq_hook.main([event, "--bridge-dir", str(self.bridge)])
        return json.loads(out.getvalue())

    def calls(self, route: str) -> list[tuple[int, Any]]:
        return [(status, body) for name, status, body in self.llmq_calls if name == route]

    def reservations(self) -> list[Any]:
        with self.llmq.engine.db.connect() as connection:
            return connection.execute("SELECT * FROM reservations").fetchall()


@pytest.fixture
def llmq(tmp_path: Path) -> Llmq:
    return Llmq(tmp_path)


@pytest.fixture
def tui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, llmq: Llmq) -> Tui:
    return Tui(tmp_path, monkeypatch, llmq)


def test_fresh_95_percent_with_turbo_off_blocks_the_prompt(tui: Tui, llmq: Llmq) -> None:
    quota = llmq.publish(95)
    verdict = tui.run()
    resets_at = quota["rateLimitsByLimitId"]["codex"]["primary"]["resetsAt"]
    assert verdict["decision"] == "block"
    assert "credit-safety margin (90%)" in verdict["reason"]
    assert time.strftime("resets at %H:%M UTC", time.gmtime(resets_at)) in verdict["reason"]
    assert "Enable Turbo on this session to use paid credits." in verdict["reason"]
    [(status, body)] = tui.calls("admissions")
    assert status == 200 and body["turbo"] is False and body["evidence"] == "controller"
    assert tui.reservations() == []
    assert [(g["state"], g["reason"], g["resets_at"]) for g in tui.gates] == [
        ("blocked", "credit_safety_margin", resets_at)
    ]


def test_turbo_on_admits_a_credit_bearing_reservation_and_stop_settles_it(
    tui: Tui, llmq: Llmq
) -> None:
    llmq.publish(95)
    tui.turbo = True
    assert tui.run() == {}
    [(status, admission)] = tui.calls("admissions")
    assert status == 200 and admission["turbo"] is True
    [(activation_status, _)] = tui.calls("activations")
    assert activation_status == 200
    [row] = tui.reservations()
    metadata = json.loads(row["metadata_json"])
    assert metadata["native_credit_bearing"] is True and row["status"] == "active"
    assert metadata.get("dispatch_authorized_at") is not None  # activated before Codex ran
    assert tui.gates == []  # verified: no indicator

    assert tui.run("stop") == {}
    settle_status, settle = tui.calls("settlements")[0]
    assert settle == {
        "turn_key": admission["turn_key"],
        "reservation_id": row["id"],
        "usage_unmeasured": True,
    }
    [row] = tui.reservations()
    metadata = json.loads(row["metadata_json"])
    if UNMEASURED_FORM:
        assert settle_status == 200 and tui.calls("cancellations") == []
        assert row["status"] == "settled" and row["actual_ppm"] == row["estimated_ppm"]
        assert metadata["native_usage_basis"] == "retained_estimate"
    else:
        # An llmq without #1328 rejects the body; the fallback cancel is refused
        # for an activated turn, so its estimate stays booked until the lease.
        assert settle_status == 422
        assert [status for status, _ in tui.calls("cancellations")] == [409]
        assert row["status"] == "active"
    assert "native_input_tokens" not in metadata  # never measured zeros


def test_stale_evidence_runs_unverified_without_a_reservation(tui: Tui, llmq: Llmq) -> None:
    llmq.publish(95, age=301)
    assert tui.run() == {}
    assert tui.reservations() == []
    assert tui.calls("activations") == []
    assert [(g["state"], g["reason"]) for g in tui.gates] == [("unverified", "receipt_stale")]
    assert tui.run("stop") == {}
    assert tui.calls("settlements") == []


def test_below_the_margin_is_verified_and_the_next_prompt_settles_a_missed_stop(
    tui: Tui, llmq: Llmq
) -> None:
    llmq.publish(33)
    assert tui.run() == {}
    assert tui.run(turn_id="turn-2") == {}
    rows = {row["id"]: row for row in tui.reservations()}
    assert len(rows) == 2
    [(_, first)] = tui.calls("settlements")[:1]
    settled = rows[first["reservation_id"]]
    if UNMEASURED_FORM:
        assert settled["status"] == "settled"
    else:
        assert settled["status"] == "active"
    assert tui.gates == []
