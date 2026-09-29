"""Codex-native forwarder keeps mirroring every turn across dropped connections.

Regression coverage for a web transcript that froze while the Codex TUI kept
working (a long-running goal-mode session): assistant items of a goal
auto-continuation turn (no ``userMessage``) made the forwarder hydrate the
whole thread with ``thread/resume``; the response exceeded the websocket
``max_size``, the connection died, and the forwarder hung forever on the
unanswerable request while ``iter_events`` never ended.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

from omnigent import codex_native_app_server
from omnigent import codex_native_forwarder as fwd
from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)

_THREAD = "thread_123"


class _FakeClient:
    """
    Codex app-server client double with per-method responses.

    :param events: Notifications yielded by ``iter_events``.
    :param responses: Response envelope per JSON-RPC method.
    :param connection_error: Value exposed as ``connection_error`` once the
        event stream ends; ``None`` models a stream that simply finished.
    """

    def __init__(
        self,
        *,
        events: list[dict[str, Any]] | None = None,
        responses: dict[str, dict[str, Any]] | None = None,
        connection_error: Exception | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.events = events or []
        self.responses = responses or {}
        self._final_connection_error = connection_error
        self.connection_error: Exception | None = None
        self.connect_error = connect_error
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        """
        Connect, or raise the configured connect error.

        :returns: None.
        """
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Record a request and return the canned response for its method.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: Canned response envelope.
        """
        self.requests.append((method, params))
        return self.responses.get(method, {"result": {"thread": {"id": _THREAD}}})

    async def iter_events(self) -> Any:
        """
        Yield the canned events, then expose the configured closure.

        :returns: Async iterator of notifications.
        """
        for event in self.events:
            yield event
            await asyncio.sleep(0)
        self.connection_error = self._final_connection_error

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        """
        Accept a JSON-RPC response (unused).

        :param request_id: Request id.
        :param result: Result payload.
        :returns: None.
        """
        del request_id, result

    async def close(self) -> None:
        """
        Mark the client closed.

        :returns: None.
        """
        self.closed = True


def _item(item_type: str, item_id: str, **extra: Any) -> dict[str, Any]:
    """
    Build a Codex history/live item.

    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :param item_id: Stable Codex item id.
    :param extra: Extra item fields.
    :returns: Item payload.
    """
    item: dict[str, Any] = {"type": item_type, "id": item_id}
    if item_type == "agentMessage":
        item["text"] = extra.pop("text", f"text of {item_id}")
    if item_type == "userMessage":
        item["content"] = [{"type": "text", "text": extra.pop("text", "hi")}]
    item.update(extra)
    return item


def _completed(turn_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """
    Build an ``item/completed`` notification.

    :param turn_id: Codex turn id.
    :param item: Codex item.
    :returns: Notification envelope.
    """
    return {
        "method": "item/completed",
        "params": {"threadId": _THREAD, "turnId": turn_id, "item": item},
    }


def _turns_list(*turns: dict[str, Any]) -> dict[str, Any]:
    """
    Build a newest-first ``thread/turns/list`` response.

    :param turns: Turns, newest first.
    :returns: Response envelope.
    """
    return {"result": {"data": list(turns), "nextCursor": None}}


def _run_forwarder(
    tmp_path: Path,
    client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reconnect_clients: list[_FakeClient] | None = None,
) -> list[dict[str, Any]]:
    """
    Run ``supervise_forwarder`` and capture Omnigent posts.

    :param tmp_path: Bridge directory.
    :param client: Initial app-server client.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param reconnect_clients: Clients handed out, in order, on reconnect.
    :returns: Posted Omnigent session events.
    """
    posted: list[dict[str, Any]] = []
    queue = list(reconnect_clients or [])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    def fake_client_for_transport(*_args: Any, **_kwargs: Any) -> _FakeClient:
        if not queue:
            return _FakeClient(connect_error=OSError("refused"))
        return queue.pop(0)

    async def no_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(fwd, "client_for_transport", fake_client_for_transport)
    monkeypatch.setattr(fwd, "_sleep", no_sleep)

    async def run() -> None:
        await asyncio.wait_for(
            fwd.supervise_forwarder(
                base_url="http://omnigent.test",
                headers={},
                session_id="conv_123",
                bridge_dir=tmp_path,
                app_server_url="ws://127.0.0.1:9",
                thread_id=_THREAD,
                client=client,  # type: ignore[arg-type]
                ap_transport=httpx.MockTransport(handler),
            ),
            timeout=10,
        )

    asyncio.run(run())
    return posted


def _assistant_texts(posted: list[dict[str, Any]]) -> list[str]:
    """
    Extract mirrored assistant message texts in post order.

    :param posted: Captured Omnigent session events.
    :returns: Assistant texts.
    """
    texts = []
    for event in posted:
        if event.get("type") != "external_conversation_item":
            continue
        data = event["data"]["item_data"]
        if data.get("role") == "assistant":
            texts.append(data["content"][0]["text"])
    return texts


def test_goal_turn_without_user_message_is_mirrored_with_one_paged_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A goal auto-continuation turn (no ``userMessage``) is mirrored item by
    item, and the user-message recovery looks it up once via a small
    ``thread/turns/list`` page — never a full-history ``thread/resume``.
    """
    goal_turn = {"id": "turn_goal", "status": "inProgress", "items": []}
    client = _FakeClient(
        events=[
            {"method": "turn/started", "params": {"threadId": _THREAD, "turn": goal_turn}},
            {
                "method": "item/started",
                "params": {
                    "threadId": _THREAD,
                    "turnId": "turn_goal",
                    "item": _item("agentMessage", "msg_1", text=""),
                },
            },
            _completed("turn_goal", _item("agentMessage", "msg_1", text="step one")),
            _completed("turn_goal", _item("agentMessage", "msg_2", text="step two")),
        ],
        responses={
            "thread/turns/list": _turns_list(
                {"id": "turn_goal", "status": "inProgress", "items": [_item("reasoning", "rs_1")]}
            )
        },
    )

    posted = _run_forwarder(tmp_path, client, monkeypatch)

    assert _assistant_texts(posted) == ["step one", "step two"]
    methods = [method for method, _ in client.requests]
    assert methods.count("thread/turns/list") == 1
    for method, params in client.requests:
        if method == "thread/resume":
            assert params.get("excludeTurns") is True


def test_forwarder_reconnects_and_backfills_after_connection_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    When the app-server connection drops, the forwarder reconnects,
    re-subscribes, and backfills the items produced meanwhile (e.g. by a
    goal turn nobody in Omnigent started) without duplicating mirrored ones.
    """
    first = _FakeClient(
        events=[_completed("turn_1", _item("agentMessage", "msg_a", text="before drop"))],
        responses={"thread/turns/list": _turns_list()},
        connection_error=ConnectionError("1009 message too big"),
    )
    gap_turn = {
        "id": "turn_2",
        "status": "inProgress",
        "items": [
            _item("agentMessage", "msg_b", text="during gap"),
            _item("commandExecution", "exec_running", status="inProgress", command="sleep 9"),
        ],
    }
    old_turn = {
        "id": "turn_1",
        "status": "completed",
        "items": [
            _item("userMessage", "user_1", text="go"),
            _item("agentMessage", "msg_a", text="before drop"),
        ],
    }
    second = _FakeClient(
        events=[_completed("turn_2", _item("agentMessage", "msg_c", text="after reconnect"))],
        responses={"thread/turns/list": _turns_list(gap_turn, old_turn)},
    )

    posted = _run_forwarder(tmp_path, first, monkeypatch, reconnect_clients=[second])

    texts = _assistant_texts(posted)
    assert texts.count("before drop") == 1
    assert "during gap" in texts
    assert texts[-1] == "after reconnect"
    assert first.closed and second.closed
    # Re-subscribed on the new connection without hydrating full history.
    assert ("thread/resume", {"threadId": _THREAD, "excludeTurns": True}) in second.requests
    # The still-running command is left for its live item/completed.
    keys = (tmp_path / fwd._SYNCED_ITEMS_LOG_NAME).read_text().split()
    assert f"{_THREAD}:turn_2:exec_running" not in keys


def test_restarted_forwarder_backfills_gap_from_persisted_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A restarted forwarder (runner restart) seeds dedup from the bridge's
    synced-items log and mirrors only what was produced while it was down.
    """
    (tmp_path / fwd._SYNCED_ITEMS_LOG_NAME).write_text(
        f"{_THREAD}:turn_1:user_1\n{_THREAD}:turn_1:msg_a\n"
    )
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path="ws://127.0.0.1:9",
            thread_id=_THREAD,
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_1",
        ),
    )
    client = _FakeClient(
        responses={
            "thread/turns/list": _turns_list(
                {
                    "id": "turn_2",
                    "status": "completed",
                    "items": [_item("agentMessage", "msg_b", text="while down")],
                },
                {
                    "id": "turn_1",
                    "status": "completed",
                    "items": [
                        _item("userMessage", "user_1", text="go"),
                        _item("agentMessage", "msg_a", text="old"),
                    ],
                },
            )
        },
    )

    posted = _run_forwarder(tmp_path, client, monkeypatch)

    assert _assistant_texts(posted) == ["while down"]
    statuses = [e["data"] for e in posted if e["type"] == "external_session_status"]
    assert statuses and statuses[-1]["status"] == "idle"
    state = read_bridge_state(tmp_path)
    assert state is not None and state.active_turn_id is None


def test_first_subscription_without_history_does_not_page_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    With nothing mirrored yet, subscribing keeps the live-only behavior:
    no history paging, no bulk re-post of an existing thread.
    """
    client = _FakeClient(responses={"thread/turns/list": _turns_list()})

    posted = _run_forwarder(tmp_path, client, monkeypatch)

    assert [method for method, _ in client.requests] == ["thread/resume"]
    assert _assistant_texts(posted) == []


def test_forwarder_gives_up_when_app_server_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A refused reconnect means the app-server exited: after bounded attempts
    the forwarder returns so the runner can tear the terminal down.
    """
    client = _FakeClient(connection_error=ConnectionError("closed"))

    _run_forwarder(tmp_path, client, monkeypatch, reconnect_clients=[])

    assert client.closed


def test_synced_items_log_is_compacted(tmp_path: Path) -> None:
    """
    The synced-items log is bounded: loading an oversized log keeps and
    rewrites only the newest keys.
    """
    log = tmp_path / fwd._SYNCED_ITEMS_LOG_NAME
    total = 2 * fwd._SYNCED_ITEMS_LOG_KEEP + 10
    log.write_text("".join(f"t:turn:{i}\n" for i in range(total)))

    keys = fwd._load_synced_item_keys(log)

    assert len(keys) == fwd._SYNCED_ITEMS_LOG_KEEP
    assert keys[-1] == f"t:turn:{total - 1}"
    assert len(log.read_text().splitlines()) == fwd._SYNCED_ITEMS_LOG_KEEP


def test_client_fails_fast_when_response_exceeds_max_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reproduces the production hang at the transport level: an app-server
    response larger than the client's websocket ``max_size`` closes the
    connection (1009). The in-flight request must raise instead of waiting
    forever, ``iter_events`` must end, and ``close()`` must not raise.
    """
    monkeypatch.setattr(codex_native_app_server, "_MAX_WEBSOCKET_MESSAGE_SIZE_BYTES", 4096)

    async def handler(ws: Any) -> None:
        async for raw in ws:
            message = json.loads(raw)
            if "id" not in message:
                continue
            if message["method"] == "initialize":
                await ws.send(json.dumps({"id": message["id"], "result": {}}))
            else:
                await ws.send(json.dumps({"id": message["id"], "result": {"x": "y" * 10000}}))

    async def run() -> None:
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            client = codex_native_app_server.CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
            await client.connect()
            assert client.connection_error is None
            with pytest.raises(codex_native_app_server.CodexAppServerConnectionClosed):
                await asyncio.wait_for(
                    client.request("thread/read", {"threadId": _THREAD, "includeTurns": True}),
                    timeout=5,
                )
            assert client.connection_error is not None
            events = [event async for event in client.iter_events()]
            assert events == []
            with pytest.raises(codex_native_app_server.CodexAppServerConnectionClosed):
                await client.request("thread/turns/list", {"threadId": _THREAD})
            await client.close()

    asyncio.run(run())
