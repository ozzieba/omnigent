"""User input cannot outrun the session stream's subscription acknowledgment."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.repl import _repl
from omnigent.server.schemas import SessionHeartbeatEvent, SessionStatusEvent


def _adapter():
    client = MagicMock()
    adapter = _repl._SessionsChatReplAdapter(client, "owned-test", session_id="owned-session")
    adapter._ensure_session = AsyncMock(return_value="owned-session")
    adapter._recover_runner_if_needed = AsyncMock()
    adapter._bind_runner_if_needed = AsyncMock()
    return adapter, client


async def _consume(adapter):
    return [event async for event in adapter.send("owned-input")]


async def test_first_input_waits_for_actual_subscription() -> None:
    """Delay registration; POST must follow ACK, then observe real completion."""
    adapter, client = _adapter()
    subscribe = asyncio.Event()
    release = asyncio.Event()
    terminal = asyncio.Queue()
    posts = []

    async def stream(sid):
        subscribe.set()
        await release.wait()
        yield SessionHeartbeatEvent(type="session.heartbeat")
        yield await terminal.get()
        await asyncio.Event().wait()

    async def post(sid, body):
        posts.append(body)
        await terminal.put(
            SessionStatusEvent(
                type="session.status", conversation_id="owned-session", status="idle"
            )
        )

    client.sessions.stream = stream
    client.sessions.post_event = post
    adapter._stream_task = asyncio.create_task(adapter._stream_pump())
    send = asyncio.create_task(_consume(adapter))
    try:
        await asyncio.wait_for(subscribe.wait(), 1)
        await asyncio.sleep(0)
        assert not posts, "input reached a no-replay stream before subscription"
        release.set()
        await asyncio.wait_for(send, 1)
        assert posts == [
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "owned-input"}],
                },
            }
        ]
    finally:
        send.cancel()
        await asyncio.gather(send, return_exceptions=True)
        await adapter.aclose()


async def test_unavailable_subscription_refuses_without_post(monkeypatch) -> None:
    """An unacknowledged stream cannot silently turn a message into a lost input."""
    adapter, client = _adapter()
    monkeypatch.setattr(_repl, "_SESSION_STREAM_READY_TIMEOUT_S", 0.01, raising=False)
    client.sessions.post_event = AsyncMock()
    try:
        with pytest.raises(RuntimeError, match="input was not sent"):
            await asyncio.wait_for(_consume(adapter), 0.2)
        client.sessions.post_event.assert_not_awaited()
    finally:
        await adapter.aclose()


async def test_reconnect_clears_previous_subscription_ack() -> None:
    """An old connection's ACK cannot admit input while the replacement opens."""
    import httpx

    adapter, client = _adapter()
    first_ready = asyncio.Event()
    break_first = asyncio.Event()
    broken = asyncio.Event()
    release_second = asyncio.Event()
    terminal = asyncio.Queue()
    subscriptions = 0
    posts = []

    async def stream(sid):
        nonlocal subscriptions
        subscriptions += 1
        if subscriptions == 1:
            yield SessionHeartbeatEvent(type="session.heartbeat")
            first_ready.set()
            await break_first.wait()
            broken.set()
            raise httpx.ReadError("owned test disconnect")
        await release_second.wait()
        yield SessionHeartbeatEvent(type="session.heartbeat")
        yield await terminal.get()
        await asyncio.Event().wait()

    async def post(sid, body):
        posts.append(body)
        await terminal.put(
            SessionStatusEvent(type="session.status", conversation_id=sid, status="idle")
        )

    client.sessions.stream = stream
    client.sessions.post_event = post
    adapter._stream_task = asyncio.create_task(adapter._stream_pump())
    send = None
    try:
        await asyncio.wait_for(first_ready.wait(), 1)
        assert adapter._stream_ready.is_set()
        break_first.set()
        await asyncio.wait_for(broken.wait(), 1)
        assert not adapter._stream_ready.is_set()
        send = asyncio.create_task(_consume(adapter))
        await asyncio.sleep(0)
        assert not posts
        release_second.set()
        await asyncio.wait_for(send, 2)
        assert len(posts) == 1 and subscriptions == 2
    finally:
        if send is not None:
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)
        await adapter.aclose()


async def test_ack_cleared_before_waiter_resumes_is_not_ready() -> None:
    """Event.wait awakening is not itself a still-live subscription."""
    adapter, _ = _adapter()
    wait = asyncio.create_task(adapter._wait_for_stream_ready())
    try:
        await asyncio.sleep(0)
        adapter._stream_ready.set()
        adapter._stream_ready.clear()
        await asyncio.sleep(0)
        assert not wait.done()
        adapter._stream_ready.set()
        await asyncio.wait_for(wait, 1)
    finally:
        wait.cancel()
        await asyncio.gather(wait, return_exceptions=True)


async def test_disconnect_during_upload_waits_before_post() -> None:
    """Attachment upload cannot consume the ACK that guards dispatch afterward."""
    from types import SimpleNamespace

    adapter, client = _adapter()
    adapter._stream_ready.set()
    uploaded = asyncio.Event()
    posts = []

    async def upload(path):
        adapter._stream_ready.clear()
        uploaded.set()
        return SimpleNamespace(id="owned-file")

    async def post(sid, body):
        posts.append(body)
        adapter._turn_done.set()

    client.files.for_session.return_value.upload = upload
    client.sessions.post_event = post

    async def consume():
        return [event async for event in adapter.send("owned-input", files=["owned.txt"])]

    send = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(uploaded.wait(), 1)
        await asyncio.sleep(0)
        assert not posts
        adapter._stream_ready.set()
        await asyncio.wait_for(send, 1)
        assert len(posts) == 1
    finally:
        send.cancel()
        await asyncio.gather(send, return_exceptions=True)
        await adapter.aclose()


async def test_repeated_stale_ack_wakeups_share_one_deadline(monkeypatch) -> None:
    """Reconnections cannot reset the finite dispatch-readiness budget."""
    from types import SimpleNamespace

    adapter, _ = _adapter()
    now = 0.0
    timeouts = []

    async def stale_wakeup(awaitable, *, timeout):
        nonlocal now
        awaitable.close()
        timeouts.append(timeout)
        now += 20.0
        adapter._stream_ready.set()
        adapter._stream_ready.clear()

    monkeypatch.setattr(_repl, "_SESSION_STREAM_READY_TIMEOUT_S", 30.0)
    monkeypatch.setattr(
        _repl,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: now),
            wait_for=stale_wakeup,
            TimeoutError=asyncio.TimeoutError,
        ),
    )
    with pytest.raises(RuntimeError, match="input was not sent"):
        await adapter._wait_for_stream_ready()
    assert timeouts == [30.0, 10.0]
