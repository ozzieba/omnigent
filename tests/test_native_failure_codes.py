"""Native StopFailure reason survives decoding and the real status wire."""

import asyncio
import json

import httpx
import pytest

from omnigent._native_failure_codes import NATIVE_FAILURE_CODES
from omnigent._native_post_delivery import post_external_session_status
from omnigent.claude_native_bridge import read_hook_events_since_with_position, record_hook_event
from omnigent.claude_native_forwarder import (
    HookForwardState,
    _forward_available_status_events,
    _ForwardDedupeState,
    _PostRetryTracker,
)


@pytest.mark.parametrize("code", sorted(NATIVE_FAILURE_CODES))
@pytest.mark.asyncio
async def test_real_hook_decoder_forwarder_and_wire(tmp_path, monkeypatch, code):
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)
    bridge = tmp_path / "bridge"
    record_hook_event(
        bridge,
        {
            "hook_event_name": "StopFailure",
            "session_id": "native-test",
            "error": code,
            "error_details": "PRIVATE-DETAIL",
            "last_assistant_message": "PRIVATE-MESSAGE",
        },
    )
    decoded = read_hook_events_since_with_position(bridge, 0)
    assert decoded.records[0].failure_code == code
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(202)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://private-fixture"
    ) as client:
        state = await asyncio.wait_for(
            _forward_available_status_events(
                client=client,
                session_id="fixture",
                bridge_dir=bridge,
                state=HookForwardState(event_cursor=0),
                retry_tracker=_PostRetryTracker(),
                dedupe=_ForwardDedupeState(),
                task_subjects={},
                task_statuses={},
                task_order=[],
            ),
            timeout=5,
        )
    assert state.event_cursor == 1 and len(sent) == 1
    assert sent[0]["data"]["status"] == "failed" and sent[0]["data"]["native_failure_code"] == code
    assert "PRIVATE" not in json.dumps(sent)


@pytest.mark.parametrize(
    "event,code",
    [
        ("StopFailure", "unknown"),
        ("StopFailure", True),
        ("StopFailure", {"private": "text"}),
        ("StopFailure", "unexpected"),
        ("Stop", "rate_limit"),
        ("SessionStart", "rate_limit"),
    ],
)
def test_hook_does_not_promote_foreign_or_unknown_failure(tmp_path, monkeypatch, event, code):
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)
    bridge = tmp_path / "bridge"
    record_hook_event(bridge, {"hook_event_name": event, "error": code})
    assert read_hook_events_since_with_position(bridge, 0).records[0].failure_code is None


@pytest.mark.asyncio
async def test_shared_status_wire_preserves_idle_and_unknown_fallback():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(202)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://private-fixture"
    ) as client:
        await post_external_session_status(
            client, session_id="fixture", status="idle", failure_code="rate_limit"
        )
        await post_external_session_status(
            client, session_id="fixture", status="failed", failure_code="unknown"
        )
    assert [x["data"] for x in sent] == [{"status": "idle"}, {"status": "failed"}]


def test_hook_record_preserves_legacy_positional_timestamp():
    from omnigent.claude_native_bridge import ClaudeHookRecord

    record = ClaudeHookRecord(1, 2, "StopFailure", 123.0, "legacy-source")
    assert record.recorded_at == 123.0
    assert record.source == "legacy-source"
    assert record.failure_code is None
