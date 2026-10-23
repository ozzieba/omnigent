"""Metadata-only discovery of older sessions with recent activity."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.tools.builtins.spawn import SysSessionListTool


def _session(identifier: str, created: int, updated: object) -> dict[str, object]:
    return {
        "id": identifier,
        "created_at": created,
        "updated_at": updated,
        "status": "idle",
        "agent_name": "researcher",
        "runner_id": None,
        "content": "synthetic private body must not be projected",
    }


async def _call(client: httpx.AsyncClient, **arguments: object) -> dict[str, object]:
    return json.loads(
        await execute_tool(
            tool_name="sys_session_list",
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id="conv_parent",
        )
    )


def _handler(rows: list[dict[str, object]], requests: list[httpx.Request]):
    async def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-existing-identity"
        if request.url.path == "/v1/sessions/conv_parent/child_sessions":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "child",
                            "tool": "reviewer",
                            "session_name": "kept",
                            "title": "reviewer:kept",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/sessions/conv_parent":
            return httpx.Response(200, json={"id": "conv_parent", "parent_session_id": None})
        if request.url.path == "/v1/sessions":
            sort_by = request.url.params.get("sort_by", "created_at")
            ordered = sorted(rows, key=lambda row: row[sort_by], reverse=True)
            after = request.url.params.get("after")
            if after is not None:
                start = next(i + 1 for i, row in enumerate(ordered) if row["id"] == after)
                ordered = ordered[start:]
            limit = int(request.url.params["limit"])
            page = ordered[:limit]
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "data": page,
                        "has_more": len(ordered) > len(page),
                        "last_id": page[-1]["id"] if page else None,
                    }
                ).encode(),
            )
        raise AssertionError(f"Unexpected request {request.url.path}")

    return serve


def _client(rows: list[dict[str, object]], requests: list[httpx.Request]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(rows, requests)),
        base_url="http://server",
        headers={"Authorization": "Bearer synthetic-existing-identity"},
    )


@pytest.mark.asyncio
async def test_revived_activity_pagination_preserves_metadata_children_and_identity() -> None:
    rows = [_session("old-revived", 1, 300), _session("new", 200, 200), _session("mid", 100, 250)]
    requests: list[httpx.Request] = []
    async with _client(rows, requests) as client:
        first = await _call(client, sort_by="updated_at", limit=1)
        second = await _call(
            client, sort_by="updated_at", limit=1, cursor=first["page"]["next_cursor"]
        )
        third = await _call(
            client, sort_by="updated_at", limit=1, cursor=second["page"]["next_cursor"]
        )
    assert [page["sessions"][0]["session_id"] for page in (first, second, third)] == [
        "old-revived",
        "mid",
        "new",
    ]
    assert [page["sessions"][0]["last_activity_at"] for page in (first, second, third)] == [
        300,
        250,
        200,
    ]
    assert first["sub_agents"] == second["sub_agents"] == third["sub_agents"]
    assert first["sub_agents"] == [
        {"agent": "reviewer", "title": "kept", "conversation_id": "child"}
    ]
    assert third["page"]["has_more"] == {"sessions": False}
    assert "next_cursor" not in third["page"]
    global_requests = [r for r in requests if r.url.path == "/v1/sessions"]
    assert all(r.url.params["sort_by"] == "updated_at" for r in global_requests)
    assert [r.url.params.get("after") for r in global_requests] == [None, "old-revived", "mid"]
    assert all("content" not in row for page in (first, second, third) for row in page["sessions"])
    assert len(requests) == 9  # Existing child/parent/list requests only; no per-row get_info.


@pytest.mark.asyncio
async def test_default_order_and_old_cursor_shape_remain_compatible() -> None:
    requests: list[httpx.Request] = []
    async with _client([_session("old", 1, 500), _session("new", 200, 200)], requests) as client:
        first = await _call(client, limit=1)
        cursor = first["page"]["next_cursor"]
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        assert payload["filters"] == {"agent_name": None}
        second = await _call(client, sort_by="created_at", limit=1, cursor=cursor)
    assert first["sessions"][0]["session_id"] == "new"
    assert second["sessions"][0]["session_id"] == "old"
    assert all("sort_by" not in r.url.params for r in requests if r.url.path == "/v1/sessions")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,changed", [("updated_at", "created_at"), ("created_at", "updated_at")]
)
async def test_cursor_cannot_change_sort_before_any_read(initial: str, changed: str) -> None:
    requests: list[httpx.Request] = []
    async with _client([_session("old", 1, 500), _session("new", 200, 200)], requests) as client:
        first = await _call(client, sort_by=initial, limit=1)
        before = len(requests)
        denied = await _call(client, sort_by=changed, cursor=first["page"]["next_cursor"])
    assert "different filter" in denied["error"]
    assert len(requests) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("sort_by", [None, True, 1, [], {}, "last_activity_at", "ASC"])
async def test_invalid_sort_is_refused_before_transport(sort_by: object) -> None:
    requests: list[httpx.Request] = []
    async with _client([], requests) as client:
        result = await _call(client, sort_by=sort_by)
    assert "sort_by" in result["error"]
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("updated", [None, True, -1, "today", float("inf"), float("nan"), 10**400])
async def test_missing_or_malformed_activity_stays_unknown(updated: object) -> None:
    requests: list[httpx.Request] = []
    async with _client([_session("unknown", 1, updated)], requests) as client:
        result = await _call(client)
    assert result["sessions"][0]["last_activity_at"] is None


def test_tool_schema_exposes_closed_sort_choice_and_default() -> None:
    schema = SysSessionListTool().get_schema()["function"]["parameters"]
    assert schema["properties"]["sort_by"] == {
        "type": "string",
        "enum": ["created_at", "updated_at"],
        "default": "created_at",
        "description": (
            "Order sessions newest-first by creation (default) or persisted activity. "
            "Reuse the same sort with its cursor. Does not change the complete sub_agents view."
        ),
    }
    assert schema["additionalProperties"] is False
