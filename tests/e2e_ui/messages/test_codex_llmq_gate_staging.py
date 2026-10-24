"""STAGING e2e: the Codex llmq gate and per-session Turbo against a REAL llmq.

Design #76 canary steps 2-4, rehearsed end to end on source builds:

* a real llmq controller (``python -m llm_quota_controller serve`` from a
  chatgpt-playground checkout on ``PYTHONPATH``), on a free 127.0.0.1 port, with
  a fresh SQLite ledger, ONE synthetic ``native-agent`` grant scoped to Codex for
  the runner, and a synthetic publisher (observer grant) that posts the receipt's
  observation batch and writes the receipt file exactly as
  ``llm_quota_controller.native_receipt`` does;
* a real Omnigent server + runner + native Codex app-server (the
  ``mocked_native_codex_session`` stack), the runner provisioned with
  ``OMNIGENT_QUOTA_ROUTE=personal-llmq`` and ``LLMQ_*``;
* Codex's mock Responses sidecar as the model provider (no Codex quota is used);
* the real web UI, driven by Playwright.

The synthetic receipt has the shape of the live publisher's receipt
(``codex_receipt.{account_response, quota_response, observed_at}`` with the
0.159.1 credited-bucket fields); only the numbers and the account id are made up.

Skipped unless ``llm_quota_controller`` is importable (CI has no llmq checkout)
and the ``mocked_native_codex_session`` prerequisites (codex CLI, sidecar) exist.
Set ``LLMQ_STAGING_EVIDENCE_DIR`` to keep per-step JSON evidence and screenshots.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

pytest.importorskip("llm_quota_controller.api")

from llm_quota_controller.codex_native_contract import (
    native_observation_batch,
)

from tests.codex_parity.helpers import (
    ev_assistant_message,
    ev_completed_with_usage,
    ev_response_created,
)
from tests.e2e_ui.conftest import MockedCodexNativeSession, _find_free_port
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _TERMINAL_VIEW,
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

#: The runner's llmq actor (``LLMQ_AGENT_ID``) and the grant's actor.
AGENT_ID = "staging-codex-runner"
PUBLISHER_ID = "staging-codex-publisher"
#: The one Codex model the credited (Turbo) contract reviews.
MODEL = "gpt-6.1-sol"
LIVE_LLMQ_PORT = 17645
_USER = '[data-testid="message-bubble"][data-role="user"]'
_TURN_TIMEOUT_MS = 180_000
_MARGIN_SENTENCE = "Codex subscription quota is at the credit-safety margin (90%)"
_TURBO_HINT = "Enable Turbo on this session to use paid credits."


# --------------------------------------------------------------------- staging llmq


def staging_receipt(
    used: float, *, credited: bool, resets_at: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Account and quota metadata in the live 0.159.1 publisher shape (synthetic values)."""
    snapshot: dict[str, Any] = {
        "credits": {"balance": "0", "hasCredits": credited, "unlimited": False},
        "individualLimit": None,
        "limitId": "codex",
        "limitName": None,
        "normalModelSlug": None,
        "planType": "pro",
        "primary": {"resetsAt": resets_at, "usedPercent": used, "windowDurationMins": 10080},
        "rateLimitReachedType": None,
        "secondary": None,
        "spendControlReached": False,
    }
    quota = {
        "accountId": "00000000-0000-4000-8000-00000000057a",
        "ordinaryUsageAllowed": True,
        "rateLimitResetCredits": {"availableCount": 0, "credits": []},
        "rateLimitUpsell": None,
        "rateLimits": copy.deepcopy(snapshot),
        "rateLimitsByLimitId": {"codex": snapshot},
    }
    return {"account": {"type": "chatgpt", "planType": "pro"}}, quota


class StagingLlmq:
    """A served llmq controller from source with a synthetic publisher and ledger reads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = _find_free_port()
        while self.port == LIVE_LLMQ_PORT:
            self.port = _find_free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.db_path = root / "controller.sqlite3"
        self.receipt_path = root / "codex-native-receipt.json"
        self.token_file = root / "agent-token"
        self.log_path = root / "llmq.log"
        self._agent_token = secrets.token_urlsafe(24)
        self._observer_token = secrets.token_urlsafe(24)
        self.token_file.write_text(self._agent_token + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        # A fixed weekly reset for the whole run, so the margin sentence is stable.
        self.resets_at = int(time.time()) + 2 * 86400 + 3 * 3600
        self._proc: subprocess.Popen[bytes] | None = None
        self.publishes: list[dict[str, Any]] = []

    def _env(self) -> dict[str, str]:
        tokens = {
            self._agent_token: {
                "role": "native-agent",
                "actor": AGENT_ID,
                "native_scope": {"workstream": "agent-infra", "harness": "codex", "model": "*"},
            },
            self._observer_token: {"role": "observer", "actor": PUBLISHER_ID},
        }
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("LLMQ_") and k not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
        }
        env.update(
            {
                "LLMQ_DB_PATH": str(self.db_path),
                "LLMQ_TOKENS_JSON": json.dumps(tokens),
                "LLMQ_CODEX_RECEIPT_FILE": str(self.receipt_path),
                "LLMQ_BIND_HOST": "127.0.0.1",
                "LLMQ_BIND_PORT": str(self.port),
            }
        )
        return env

    def start(self) -> None:
        log = open(self.log_path, "ab")  # noqa: SIM115
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "llm_quota_controller", "serve"],
            env=self._env(),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"staging llmq exited {self._proc.returncode}: "
                    f"{self.log_path.read_text()[-2000:]}"
                )
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{self.url}/healthz", timeout=1).status_code == 200:
                    return
            time.sleep(0.2)
        raise RuntimeError("staging llmq did not become healthy")

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        self._proc = None

    def runner_env(self) -> dict[str, str]:
        return {
            "OMNIGENT_QUOTA_ROUTE": "personal-llmq",
            "LLMQ_CONTROLLER_URL": self.url,
            "LLMQ_TOKEN_FILE": str(self.token_file),
            "LLMQ_AGENT_ID": AGENT_ID,
        }

    def bearer(self) -> str:
        return self._agent_token

    def publish(self, used: float, *, credited: bool = True, age: float = 0.0) -> dict[str, Any]:
        """Publish a receipt like the native publisher: observation batch, then the file."""
        observed_at = time.time() - age
        account, quota = staging_receipt(used, credited=credited, resets_at=self.resets_at)
        batch = native_observation_batch(account, quota, observed_at=observed_at)
        resp = httpx.post(
            f"{self.url}/v1/observations",
            json=batch.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {self._observer_token}"},
            timeout=5,
        )
        tmp = self.receipt_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "codex_receipt": {
                        "account_response": account,
                        "quota_response": quota,
                        "observed_at": observed_at,
                    }
                }
            ),
            encoding="utf-8",
        )
        tmp.chmod(0o600)
        os.replace(tmp, self.receipt_path)
        record = {
            "used_percent": used,
            "credited": credited,
            "age_s": age,
            "observed_at": observed_at,
            "observation_http": resp.status_code,
        }
        self.publishes.append(record)
        return record

    # ------------------------------------------------------------------ ledger reads

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        uri = f"file:{self.db_path}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(sql, params)]

    def mark(self) -> dict[str, float]:
        seq = self.query("SELECT COALESCE(MAX(seq), 0) AS s FROM audit_events")
        return {"seq": seq[0]["s"] if seq else 0, "t": time.time()}

    def native_events(self, mark: dict[str, float]) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT seq, event_type, actor, subject, detail_json, created_at FROM audit_events "
            "WHERE seq > ? AND event_type IN ('native_turn_refused', 'native_turn_unverified') "
            "ORDER BY seq",
            (mark["seq"],),
        )
        return [
            {
                "event_type": r["event_type"],
                "subject": r["subject"],
                "detail": json.loads(r["detail_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def decisions(self, mark: dict[str, float]) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT request_id, decision_json, created_at FROM admission_decisions "
            "WHERE created_at >= ? ORDER BY created_at",
            (mark["t"] - 1,),
        )
        return [
            {"request_id": r["request_id"], "created_at": r["created_at"]}
            | {
                k: v
                for k, v in json.loads(r["decision_json"]).items()
                if k
                in {
                    "allowed",
                    "reason",
                    "reservation_id",
                    "credit_bearing",
                    "verified",
                    "unverified_reason",
                    "resolved_via",
                    "estimated_ppm",
                    "admission_delay_seconds",
                    "admission_not_before",
                }
            }
            for r in rows
        ]

    def reservations(self, mark: dict[str, float]) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT id, request_id, model, status, estimated_ppm, actual_ppm, created_at, "
            "settled_at, metadata_json FROM reservations WHERE created_at >= ? "
            "ORDER BY created_at",
            (mark["t"] - 1,),
        )
        keep = {
            "native_credit_bearing",
            "dispatch_authorized_at",
            "dispatched_at",
            "native_usage_basis",
            "native_input_tokens",
            "native_output_tokens",
            "native_turn_actor",
            "native_attempt",
        }
        out = []
        for r in rows:
            meta = json.loads(r.pop("metadata_json") or "{}")
            r["metadata"] = {k: v for k, v in meta.items() if k in keep}
            r["metadata_keys"] = sorted(meta)
            out.append(r)
        return out

    def ledger_delta(self, mark: dict[str, float]) -> dict[str, Any]:
        return {
            "native_events": self.native_events(mark),
            "decisions": self.decisions(mark),
            "reservations": self.reservations(mark),
        }

    def wait_settled(self, mark: dict[str, float], timeout: float = 30.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        rows = self.reservations(mark)
        while time.monotonic() < deadline:
            rows = self.reservations(mark)
            if rows and all(r["status"] != "active" for r in rows):
                return rows
            time.sleep(0.5)
        return rows


class HungController:
    """A TCP listener on the controller's port that accepts and never answers."""

    def __init__(self, port: int) -> None:
        self.connections = 0
        self.requests: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            self._held.append(conn)
            conn.settimeout(2)
            with contextlib.suppress(OSError):
                head = conn.recv(4096).decode("latin-1", "replace").split("\r\n", 1)[0]
                self.requests.append(head)

    def close(self) -> None:
        self._stop.set()
        for conn in self._held:
            with contextlib.suppress(OSError):
                conn.close()
        with contextlib.suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=5)


def _evidence_root() -> Path | None:
    root = os.environ.get("LLMQ_STAGING_EVIDENCE_DIR")
    if not root:
        return None
    out = Path(root)
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def staging_llmq(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[StagingLlmq]:
    """A fresh staging controller and ledger per test, so pacing never carries over."""
    llmq = StagingLlmq(tmp_path_factory.mktemp("llmq-staging"))
    llmq.start()
    try:
        yield llmq
    finally:
        llmq.stop()
        out = _evidence_root()
        if out is not None:
            name = request.node.name.split("[")[0]
            everything = {"seq": 0, "t": 0.0}
            (out / f"{name}-ledger.json").write_text(
                json.dumps(
                    {
                        "publishes": llmq.publishes,
                        **llmq.ledger_delta(everything),
                        "audit_event_counts": llmq.query(
                            "SELECT event_type, COUNT(*) AS n FROM audit_events "
                            "GROUP BY event_type ORDER BY event_type"
                        ),
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            if llmq.log_path.exists():
                (out / f"{name}-llmq.log").write_text(
                    llmq.log_path.read_text(errors="replace"), encoding="utf-8"
                )


_LOG_MARKERS = ("llmq", "quota", "turbo")


@pytest.fixture(autouse=True)
def _keep_gate_logs(request: pytest.FixtureRequest) -> Iterator[None]:
    """Copy the gate's server/runner log lines out of the suite's temp data dir.

    ``tests/conftest.py`` deletes ``OMNIGENT_DATA_DIR`` (where the spawned
    server and runner write their logs) at session end.
    """
    yield
    out = _evidence_root()
    data_dir = os.environ.get("OMNIGENT_DATA_DIR")
    if out is None or not data_dir:
        return
    lines: list[str] = []
    for path in sorted(Path(data_dir).glob("logs/**/*.log")):
        with contextlib.suppress(OSError):
            for line in path.read_text(errors="replace").splitlines():
                if any(marker in line.lower() for marker in _LOG_MARKERS):
                    lines.append(f"{path.parent.name}/{path.name}: {line[:600]}")
    name = request.node.name.split("[")[0]
    (out / f"{name}-gate-log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def mocked_codex_runner_env(staging_llmq: StagingLlmq) -> dict[str, str]:
    """Provision the runner's llmq gate against the staging controller."""
    return staging_llmq.runner_env()


# --------------------------------------------------------------------------- helpers


class Evidence:
    """Per-test step log, written as JSON when LLMQ_STAGING_EVIDENCE_DIR is set."""

    def __init__(self, name: str) -> None:
        root = os.environ.get("LLMQ_STAGING_EVIDENCE_DIR")
        self.dir = Path(root) if root else None
        self.name = name
        self.steps: list[dict[str, Any]] = []
        if self.dir is not None:
            self.dir.mkdir(parents=True, exist_ok=True)

    def add(self, step: str, **data: Any) -> None:
        self.steps.append({"step": step, **data})
        self.flush()

    def shot(self, page: Page, label: str) -> str | None:
        if self.dir is None:
            return None
        path = self.dir / f"{self.name}-{label}.png"
        with contextlib.suppress(Exception):
            page.screenshot(path=str(path), full_page=False)
        return str(path)

    def flush(self) -> None:
        if self.dir is not None:
            (self.dir / f"{self.name}.json").write_text(
                json.dumps(self.steps, indent=2, default=str), encoding="utf-8"
            )


def _reply(n: int, text: str) -> list[dict[str, Any]]:
    """One mocked model response: an assistant message with measured usage."""
    return [
        ev_response_created(f"resp-stage-{n}"),
        ev_assistant_message(f"msg-stage-{n}", text),
        ev_completed_with_usage(
            f"resp-stage-{n}",
            input_tokens=1200,
            cached_input_tokens=200,
            output_tokens=80,
            reasoning_output_tokens=0,
            total_tokens=1280,
        ),
    ]


def _open_session(page: Page, session: MockedCodexNativeSession) -> None:
    page.goto(f"{session.base_url}/c/{session.session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)


def _gear_summary(page: Page) -> str:
    """The composer gear's hover summary (where the quota indicator lives)."""
    page.mouse.move(0, 0)
    page.get_by_test_id("composer-config-gear").hover()
    tooltip = page.get_by_test_id("composer-config-gear-tooltip")
    expect(tooltip.first).to_be_visible(timeout=10_000)
    text = tooltip.first.inner_text()
    page.mouse.move(0, 0)
    return " ".join(text.split())


def _expect_gear_summary(
    page: Page, predicate: Callable[[str], bool], timeout: float = 20.0
) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            text = _gear_summary(page)
            if predicate(text):
                return text
        time.sleep(1.0)
    raise AssertionError(f"gear summary never matched; last={text!r}")


def _set_turbo(page: Page, on: bool) -> dict[str, Any]:
    """Flip the composer's Turbo switch in the session config modal."""
    page.get_by_test_id("composer-config-gear").click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible(timeout=10_000)
    state = page.get_by_test_id("composer-config-turbo-state")
    before = state.inner_text()
    patched: dict[str, Any] = {}
    if (before.startswith("On")) != on:
        with page.expect_response(
            lambda r: r.request.method == "PATCH" and "/v1/sessions/" in r.url
        ) as info:
            page.get_by_test_id("composer-config-turbo").click()
        patched = {"status": info.value.status, "body": info.value.request.post_data}
    expect(state).to_have_text("On: paid credits allowed" if on else "Off", timeout=10_000)
    after = state.inner_text()
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("composer-config-modal")).to_have_count(0, timeout=10_000)
    return {"before": before, "after": after, "patch": patched}


def _session_json(session: MockedCodexNativeSession) -> dict[str, Any]:
    resp = httpx.get(f"{session.base_url}/v1/sessions/{session.session_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _runner_gate_lines(session: MockedCodexNativeSession) -> list[str]:
    path = session.runner_log_path
    if path is None or not path.exists():
        return []
    return [
        line[:400]
        for line in path.read_text(errors="replace").splitlines()
        if "llmq gate" in line or "llmq_gate" in line
    ]


def _request_summaries(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Path, model and the last user text of every mocked provider request."""
    out = []
    for req in requests:
        body = req.get("body") or {}
        texts = [
            part.get("text", "")
            for item in body.get("input") or []
            if isinstance(item, dict) and item.get("role") == "user"
            for part in item.get("content") or []
            if isinstance(part, dict)
        ]
        out.append(
            {
                "path": req.get("path"),
                "model": body.get("model"),
                "input_items": len(body.get("input") or []),
                "last_user_text": texts[-1][:120] if texts else None,
                "instructions_head": str(body.get("instructions") or "")[:80],
            }
        )
    return out


def _wait_requests(session: MockedCodexNativeSession, count: int, timeout_s: float) -> float:
    """Block until the mock provider has ``count`` requests; returns the monotonic time."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if len(session.sidecar.requests(min_count=0, timeout_ms=100)) >= count:
            return time.monotonic()
        time.sleep(0.1)
    raise AssertionError(
        f"mock provider has {len(session.sidecar.requests())} requests, expected {count}"
    )


def _provider_requests(session: MockedCodexNativeSession) -> list[dict[str, Any]]:
    return session.sidecar.requests(min_count=0, timeout_ms=100)


def _requests_with(requests: list[dict[str, Any]], text: str) -> int:
    """How many provider requests carry ``text`` in their newest user message."""
    return sum(1 for r in _request_summaries(requests) if r["last_user_text"] == text)


def _turn(
    page: Page,
    session: MockedCodexNativeSession,
    llmq: StagingLlmq,
    text: str,
) -> dict[str, Any]:
    """Send one message, wait for its mocked reply; return timing and provider requests.

    Order-independent: Codex may make more than one provider request per turn,
    so the reply is "a new assistant bubble", not a specific scripted text.
    """
    mark = llmq.mark()
    before = len(_provider_requests(session))
    bubbles = page.locator(_ASSISTANT).count()
    started = time.monotonic()
    _send(page, text)
    arrived = _wait_requests(session, before + 1, _TURN_TIMEOUT_MS / 1000)
    deadline = time.monotonic() + _TURN_TIMEOUT_MS / 1000
    while page.locator(_ASSISTANT).count() <= bubbles and time.monotonic() < deadline:
        time.sleep(0.25)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)
    requests = _provider_requests(session)
    new = requests[before:]
    return {
        "mark": mark,
        "send_to_provider_s": round(arrived - started, 2),
        "new_provider_requests": len(new),
        "new_request_summaries": _request_summaries(new),
        "requests_with_text": _requests_with(new, text),
        "first_new_request_model": new[0]["body"].get("model") if new else None,
        "assistant_bubbles": page.locator(_ASSISTANT).count(),
        "assistant_bubbles_before": bubbles,
    }


# ------------------------------------------------------------------------------ tests


def test_staging_admission_matrix(staging_llmq: StagingLlmq) -> None:
    """Canary step 1 analogue: the runner's real admission call against the served llmq.

    Records what llmq needs for a hold and for a credit-bearing Turbo admission:
    the margin and Turbo apply only to a CREDITED account (``hasCredits: true``).
    """
    import asyncio

    from omnigent.runner.llmq_gate import GateConfig, admit_codex_turn, cancel_codex_turn

    cfg = GateConfig.from_env(staging_llmq.runner_env())
    assert cfg is not None
    evidence = Evidence("matrix")
    rows: list[dict[str, Any]] = []

    async def run() -> None:
        async with httpx.AsyncClient(trust_env=False) as client:
            for credited, used in [(True, 17), (True, 89), (True, 95), (False, 95), (False, 100)]:
                staging_llmq.publish(used, credited=credited)
                for turbo in (False, True):
                    key = secrets.token_hex(32)
                    decision = await admit_codex_turn(
                        client, cfg, turn_key=key, attempt=0, model=MODEL, turbo=turbo
                    )
                    rows.append(
                        {
                            "credited": credited,
                            "used": used,
                            "turbo": turbo,
                            "state": decision.state,
                            "reason": decision.reason,
                            "credit_bearing": decision.credit_bearing,
                            "reservation": decision.reservation_id is not None,
                            "resets_at": decision.resets_at,
                            "admission_delay_s": decision.admission_delay_seconds,
                        }
                    )
                    if decision.reservation_id is not None:
                        await cancel_codex_turn(client, cfg, decision, turn_key=key)

    asyncio.run(run())
    evidence.add("matrix", rows=rows)
    table = {(r["credited"], r["used"], r["turbo"]): r for r in rows}
    for used in (17, 89):
        for turbo in (False, True):
            row = table[(True, used, turbo)]
            assert (row["state"], row["credit_bearing"]) == ("verified", False), row
    assert table[(True, 95, False)]["state"] == "blocked"
    assert table[(True, 95, False)]["reason"] == "credit_safety_margin"
    assert table[(True, 95, False)]["resets_at"] == staging_llmq.resets_at
    assert table[(True, 95, True)]["state"] == "verified"
    assert table[(True, 95, True)]["credit_bearing"] is True
    # Uncredited: no 90% margin (nothing to overflow into); used up is exhausted
    # and Turbo cannot buy past it.
    assert table[(False, 95, False)]["state"] == "verified"
    assert table[(False, 95, True)]["credit_bearing"] is False
    assert table[(False, 100, False)]["reason"] == "quota_exhausted"
    assert table[(False, 100, True)]["reason"] == "quota_exhausted"


@pytest.mark.timeout(900)
@pytest.mark.parametrize(
    "mocked_native_codex_session",
    [{"model": MODEL, "responses": [_reply(n, f"STAGE_REPLY_{n}") for n in range(1, 16)]}],
    indirect=True,
)
def test_staging_verified_unreachable_hung_stale_then_fresh(
    page: Page,
    mocked_native_codex_session: MockedCodexNativeSession,
    staging_llmq: StagingLlmq,
) -> None:
    """S1 verified; S3 controller down and hung; S4 stale receipt, then fresh again."""
    session = mocked_native_codex_session
    llmq = staging_llmq
    evidence = Evidence("s1-s3-s4")
    _open_session(page, session)

    # S1: below the margin, Turbo off -> verified, reservation activated + settled.
    llmq.publish(17, credited=True)
    t1 = _turn(page, session, llmq, "S1 below margin turn")
    s1_res = llmq.wait_settled(t1["mark"])
    summary1 = _gear_summary(page)
    evidence.add(
        "S1",
        **t1,
        ledger=llmq.ledger_delta(t1["mark"]),
        settled=s1_res,
        gear=summary1,
        shot=evidence.shot(page, "s1"),
    )
    assert t1["requests_with_text"] == 1
    assert t1["first_new_request_model"] == MODEL
    assert "Quota:" not in summary1
    assert len(s1_res) == 1
    assert s1_res[0]["status"] == "settled"
    assert s1_res[0]["metadata"].get("dispatch_authorized_at") is not None
    assert s1_res[0]["metadata"].get("native_credit_bearing") is not True
    assert s1_res[0]["metadata"].get("native_input_tokens") != 0
    assert llmq.native_events(t1["mark"]) == []

    # S3a: controller stopped -> the turn runs, indicator "llmq unreachable".
    llmq.stop()
    t3 = _turn(page, session, llmq, "S3 controller down turn")
    summary3 = _expect_gear_summary(page, lambda s: "Unverified (llmq unreachable)" in s)
    shot3 = evidence.shot(page, "s3a")
    # Observation only: the indicator rides on live session.status edges, not on
    # the session snapshot, so a reload drops it until the next gated turn.
    page.reload()
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
    _ensure_chat_view(page)
    summary3_reload: str | None = None
    with contextlib.suppress(Exception):
        summary3_reload = _gear_summary(page)
    evidence.add(
        "S3a",
        **t3,
        gear=summary3,
        gear_after_reload=summary3_reload,
        shot=shot3,
    )
    assert t3["requests_with_text"] == 1
    assert t3["send_to_provider_s"] < 15

    # S3b: controller hung (accepts, never answers) -> 5 s timeout, one connection.
    hung = HungController(llmq.port)
    try:
        t3b = _turn(page, session, llmq, "S3 controller hung turn")
        summary3b = _expect_gear_summary(page, lambda s: "Unverified (llmq unreachable)" in s)
        hung_conns, hung_reqs = hung.connections, list(hung.requests)
    finally:
        hung.close()
    evidence.add(
        "S3b",
        **t3b,
        gear=summary3b,
        hung_connections=hung_conns,
        hung_requests=hung_reqs,
        baseline_send_to_provider_s=t1["send_to_provider_s"],
    )
    assert hung_conns == 1, hung_reqs
    assert hung_reqs and hung_reqs[0].startswith("POST /v1/native-turns/admissions")
    assert t3b["send_to_provider_s"] >= 5.0
    assert t3b["send_to_provider_s"] < t1["send_to_provider_s"] + 9.0

    # The controller comes back on the same ledger; the outage left no trace in it.
    llmq.start()
    outage_rows = llmq.query(
        "SELECT COUNT(*) AS n FROM audit_events WHERE seq > ?", (t3["mark"]["seq"],)
    )
    assert outage_rows[0]["n"] == 0

    # S4a: stale receipt (observed 400 s ago) -> unverified, no reservation, audit only.
    llmq.publish(17, credited=True, age=400)
    t4 = _turn(page, session, llmq, "S4 stale receipt turn")
    summary4 = _expect_gear_summary(page, lambda s: "Unverified (receipt stale)" in s)
    delta4 = llmq.ledger_delta(t4["mark"])
    evidence.add("S4a", **t4, gear=summary4, ledger=delta4, shot=evidence.shot(page, "s4a"))
    assert [e["event_type"] for e in delta4["native_events"]] == ["native_turn_unverified"]
    assert delta4["native_events"][0]["detail"]["reason"] == "receipt_stale"
    assert delta4["decisions"] == []
    assert delta4["reservations"] == []

    # S4b: fresh receipt, same session -> verified reservation, indicator cleared.
    llmq.publish(17, credited=True)
    t5 = _turn(page, session, llmq, "S4 fresh again turn")
    s5_res = llmq.wait_settled(t5["mark"])
    summary5 = _expect_gear_summary(page, lambda s: "Quota:" not in s)
    evidence.add(
        "S4b",
        **t5,
        gear=summary5,
        ledger=llmq.ledger_delta(t5["mark"]),
        settled=s5_res,
        runner_gate_log=_runner_gate_lines(session),
    )
    assert len(s5_res) == 1 and s5_res[0]["status"] == "settled"
    assert s5_res[0]["metadata"].get("dispatch_authorized_at") is not None
    assert llmq.native_events(t5["mark"]) == []
    # Every verified turn was admitted exactly once (the TUI hook skipped them).
    all_decisions = llmq.decisions({"t": 0})
    assert len([d for d in all_decisions if d["created_at"] >= t1["mark"]["t"] - 1]) == 2


@pytest.mark.timeout(900)
@pytest.mark.parametrize(
    "mocked_native_codex_session",
    [{"model": MODEL, "responses": [_reply(n, f"TURBO_REPLY_{n}") for n in range(1, 9)]}],
    indirect=True,
)
def test_staging_margin_hold_then_turbo_releases_once(
    page: Page,
    mocked_native_codex_session: MockedCodexNativeSession,
    staging_llmq: StagingLlmq,
) -> None:
    """S2 hold at the margin + live Turbo release; S5 Turbo persistence."""
    session = mocked_native_codex_session
    llmq = staging_llmq
    evidence = Evidence("s2-s5")

    # S5: a new session starts with Turbo off.
    assert _session_json(session).get("turbo") is False
    _open_session(page, session)
    turbo_initial = _set_turbo(page, False)
    assert turbo_initial["before"] == "Off"

    # S2: credited, 95% used, fresh -> the turn is held before the model is called.
    llmq.publish(95, credited=True)
    mark = llmq.mark()
    held_text = f"S2 held prompt {secrets.token_hex(4)}"
    _send(page, held_text)
    expect(page.locator(_USER, has_text=held_text).first).to_be_visible(timeout=30_000)
    working = page.locator(_WORKING)
    expect(working).to_contain_text(_MARGIN_SENTENCE, timeout=60_000)
    expect(working).to_contain_text(_TURBO_HINT, timeout=5_000)
    blocked_label = " ".join(working.first.inner_text().split())
    gear_blocked = _gear_summary(page)
    shot_blocked = evidence.shot(page, "s2-held")
    # Hold across one full re-check so a second attempt is audited.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and len(llmq.native_events(mark)) < 2:
        time.sleep(1)
    held_events = llmq.native_events(mark)
    held_reqs = _provider_requests(session)
    held_requests = _requests_with(held_reqs, held_text)
    evidence.add(
        "S2-held",
        working_label=blocked_label,
        gear=gear_blocked,
        provider_requests_total=len(held_reqs),
        provider_requests_with_held_text=held_requests,
        request_summaries=_request_summaries(held_reqs),
        ledger=llmq.ledger_delta(mark),
        shot=shot_blocked,
    )
    assert held_requests == 0
    assert "resets at" in blocked_label
    assert "Quota:" in gear_blocked and _MARGIN_SENTENCE in gear_blocked
    assert len(held_events) >= 2
    assert all(e["event_type"] == "native_turn_refused" for e in held_events)
    assert all(e["detail"]["reason"] == "credit_safety_margin" for e in held_events)
    assert all(e["detail"]["turbo"] is False for e in held_events)
    assert len({e["subject"] for e in held_events}) == len(held_events)  # fresh attempt ids
    assert llmq.reservations(mark) == []
    assert llmq.decisions(mark) == []

    # Toggle Turbo ON in the UI while the turn is held -> released once.
    bubbles = page.locator(_ASSISTANT).count()
    toggled = time.monotonic()
    turbo_on = _set_turbo(page, True)
    released = _wait_requests(session, 1, 60)
    deadline = time.monotonic() + _TURN_TIMEOUT_MS / 1000
    while page.locator(_ASSISTANT).count() <= bubbles and time.monotonic() < deadline:
        time.sleep(0.25)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)
    requests = _provider_requests(session)
    settled = llmq.wait_settled(mark)
    chip_visible = page.get_by_test_id("composer-turbo-chip").is_visible()
    evidence.add(
        "S2-turbo-release",
        turbo_toggle=turbo_on,
        toggle_to_provider_s=round(released - toggled, 2),
        provider_requests=len(requests),
        request_summaries=_request_summaries(requests),
        requests_with_held_text=_requests_with(requests, held_text),
        assistant_bubbles=page.locator(_ASSISTANT).count(),
        ledger=llmq.ledger_delta(mark),
        settled=settled,
        chip_visible=chip_visible,
        shot=evidence.shot(page, "s2-released"),
    )
    assert turbo_on["patch"].get("status") == 200
    assert released - toggled <= 40
    assert _requests_with(requests, held_text) == 1
    decisions = llmq.decisions(mark)
    assert len(decisions) == 1 and decisions[0]["allowed"] is True
    assert len(settled) == 1
    assert settled[0]["metadata"].get("native_credit_bearing") is True
    assert settled[0]["metadata"].get("dispatch_authorized_at") is not None
    assert settled[0]["status"] == "settled"
    assert settled[0]["metadata"].get("native_input_tokens") != 0
    assert chip_visible

    # Released ONCE: one more re-check period brings no second request or admission.
    time.sleep(35)
    later = _provider_requests(session)
    evidence.add(
        "S2-once",
        provider_requests=len(later),
        request_summaries=_request_summaries(later),
        requests_with_held_text=_requests_with(later, held_text),
        decisions=len(llmq.decisions(mark)),
        native_events_after_release=len(llmq.native_events(mark)) - len(held_events),
    )
    # Codex's own background calls (thread title, ...) are not runner turns and
    # are not gated; the invariant is one delivery of the held prompt, one admission.
    assert _requests_with(later, held_text) == 1
    assert len(llmq.decisions(mark)) == 1
    assert len(llmq.native_events(mark)) == len(held_events)

    # S5: Turbo persists across a reload and on the session record.
    page.reload()
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
    _ensure_chat_view(page)
    expect(page.get_by_test_id("composer-turbo-chip")).to_be_visible(timeout=30_000)
    record = _session_json(session)
    evidence.add("S5-reload", turbo=record.get("turbo"), shot=evidence.shot(page, "s5-reload"))
    assert record.get("turbo") is True

    # S5: a new chat created with Turbo on (the NewChatDialog's create body).
    agent_id = record.get("agent_id")
    created = httpx.post(
        f"{session.base_url}/v1/sessions",
        json={"agent_id": agent_id, "turbo": True},
        timeout=30,
    )
    created_body = created.json() if created.status_code < 300 else {}
    # JSON create answers a SessionResponse (``id``); the multipart path ``session_id``.
    new_id = created_body.get("id") or created_body.get("session_id")
    new_turbo = None
    if new_id:
        new_turbo = (
            httpx.get(f"{session.base_url}/v1/sessions/{new_id}", timeout=10).json().get("turbo")
        )
        httpx.delete(f"{session.base_url}/v1/sessions/{new_id}", timeout=10)
    evidence.add(
        "S5-new-chat",
        agent_id_present=bool(agent_id),
        create_status=created.status_code,
        create_response_turbo=created_body.get("turbo"),
        new_turbo=new_turbo,
        runner_gate_log=_runner_gate_lines(session),
    )
    assert created.status_code < 300, created.text[:300]
    assert new_turbo is True


@pytest.mark.timeout(900)
@pytest.mark.parametrize(
    "mocked_native_codex_session",
    [{"model": MODEL, "responses": [_reply(n, f"TUI_REPLY_{n}") for n in range(1, 9)]}],
    indirect=True,
)
def test_staging_tui_prompt_hook_blocks_then_turbo_admits(
    page: Page,
    mocked_native_codex_session: MockedCodexNativeSession,
    staging_llmq: StagingLlmq,
) -> None:
    """Canary step 5: a prompt typed into the Codex TUI goes through the 3b hook.

    At the margin with Turbo off the UserPromptSubmit hook blocks the prompt
    (the model is never called); with Turbo on the next typed prompt is
    admitted on a credit-bearing reservation, activated, and settled by Stop.
    """
    session = mocked_native_codex_session
    llmq = staging_llmq
    evidence = Evidence("s6-tui")
    llmq.publish(95, credited=True)
    _open_session(page, session)
    _open_terminal_view(page)
    _wait_terminal_connected(page)

    mark = llmq.mark()
    blocked_text = f"S6 tui blocked {secrets.token_hex(4)}"
    _type_into_tui(page, blocked_text)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not llmq.native_events(mark):
        time.sleep(0.5)
    time.sleep(3)
    blocked_events = llmq.native_events(mark)
    blocked_reqs = _provider_requests(session)
    terminal_text = " ".join(page.locator(_TERMINAL_VIEW).last.inner_text().split())
    shot_blocked = evidence.shot(page, "s6-tui-blocked")
    _ensure_chat_view(page)
    gear_blocked = _gear_summary(page)
    evidence.add(
        "S6-blocked",
        native_events=blocked_events,
        decisions=llmq.decisions(mark),
        reservations=llmq.reservations(mark),
        provider_requests_with_text=_requests_with(blocked_reqs, blocked_text),
        request_summaries=_request_summaries(blocked_reqs),
        terminal_mentions_margin=_MARGIN_SENTENCE[:40] in terminal_text,
        terminal_tail=terminal_text[-600:],
        gear=gear_blocked,
        shot=shot_blocked,
    )
    assert [e["event_type"] for e in blocked_events] == ["native_turn_refused"]
    assert blocked_events[0]["detail"]["reason"] == "credit_safety_margin"
    assert blocked_events[0]["detail"]["turbo"] is False
    assert _requests_with(blocked_reqs, blocked_text) == 0
    assert llmq.decisions(mark) == [] and llmq.reservations(mark) == []

    turbo_on = _set_turbo(page, True)
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    mark2 = llmq.mark()
    admitted_text = f"S6 tui turbo {secrets.token_hex(4)}"
    _type_into_tui(page, admitted_text)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if _requests_with(_provider_requests(session), admitted_text) >= 1:
            break
        time.sleep(0.5)
    settled = llmq.wait_settled(mark2, timeout=90)
    admitted_reqs = _provider_requests(session)
    evidence.add(
        "S6-turbo",
        turbo_toggle=turbo_on,
        provider_requests_with_text=_requests_with(admitted_reqs, admitted_text),
        request_summaries=_request_summaries(admitted_reqs),
        ledger=llmq.ledger_delta(mark2),
        settled=settled,
        shot=evidence.shot(page, "s6-tui-turbo"),
        runner_gate_log=_runner_gate_lines(session),
    )
    assert _requests_with(admitted_reqs, admitted_text) == 1
    decisions = llmq.decisions(mark2)
    assert len(decisions) == 1 and decisions[0]["allowed"] is True
    assert llmq.native_events(mark2) == []
    assert len(settled) == 1 and settled[0]["status"] == "settled"
    assert settled[0]["metadata"].get("native_credit_bearing") is True
    assert settled[0]["metadata"].get("dispatch_authorized_at") is not None
    assert settled[0]["metadata"].get("native_usage_basis") == "retained_estimate"
