"""Metadata-only session liveness preserves absent/malformed authority."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import _session_get_info_via_rest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire,expected",
    [
        ({}, (None, None, None, None, None)),
        (
            {
                "host_resumable": False,
                "host_online": False,
                "runner_online": False,
                "active_response_id": None,
                "pending_inputs": [],
            },
            (False, False, False, False, 0),
        ),
        (
            {
                "host_resumable": True,
                "host_online": False,
                "runner_online": False,
                "active_response_id": None,
                "pending_inputs": [],
            },
            (True, False, False, False, 0),
        ),
        (
            {
                "host_resumable": False,
                "host_online": True,
                "runner_online": True,
                "active_response_id": "public-response-id",
                "pending_inputs": [{"content": "PRIVATE_PENDING_DO_NOT_EXPORT"}],
            },
            (False, True, True, True, 1),
        ),
        (
            {
                "host_resumable": 0,
                "host_online": 1,
                "runner_online": "false",
                "active_response_id": "",
                "pending_inputs": [None],
            },
            (None, None, None, None, None),
        ),
    ],
)
async def test_runtime_authority_metadata(wire, expected):
    def handle(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/sessions/public-session-id"
        assert dict(request.url.params) == {"include_items": "false", "include_liveness": "true"}
        return httpx.Response(200, json={"id": "public-session-id", "status": "idle", **wire})

    async with httpx.AsyncClient(
        base_url="https://fixture.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        raw = await _session_get_info_via_rest(
            {"session_id": "public-session-id"}, "other-session", client
        )
    out = json.loads(raw)
    assert (
        tuple(
            out[k]
            for k in (
                "host_resumable",
                "host_online",
                "liveness_runner_online",
                "active_response_present",
                "pending_input_count",
            )
        )
        == expected
    )
    assert "PRIVATE_PENDING_DO_NOT_EXPORT" not in raw
    assert "pending_inputs" not in out and "active_response_id" not in out
