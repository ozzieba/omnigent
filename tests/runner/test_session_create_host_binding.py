"""``sys_session_create`` can bind a child to an explicit host/workspace."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import _build_session_create_body, _execute_session_create
from omnigent.tools.builtins.spawn import SysSessionCreateTool

pytestmark = pytest.mark.asyncio

_HOST = "ab071af4656c4ad7ae37d0244437f8bf"


async def test_body_carries_host_binding() -> None:
    body = _build_session_create_body(
        "ag_docloop",
        "conv_parent",
        "audit",
        None,
        model="gpt-6-luna",
        reasoning_effort="max",
        host_id=_HOST,
        workspace="/srv/lp",
    )
    assert body["parent_session_id"] == "conv_parent"
    assert body["host_id"] == _HOST
    assert body["workspace"] == "/srv/lp"


async def test_body_omits_workspace_without_host() -> None:
    body = _build_session_create_body("ag_x", "conv_parent", None, None, workspace="/srv/lp")
    assert "host_id" not in body
    assert "workspace" not in body


async def test_execute_posts_host_binding() -> None:
    seen: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            201, json={"id": "conv_child", "agent_name": "docloop", "status": "idle"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://server"
    ) as client:
        result = json.loads(
            await _execute_session_create(
                {"agent_id": "ag_docloop", "host_id": _HOST, "workspace": "/srv/lp"},
                server_client=client,
                conversation_id="conv_parent",
                publish_event=None,
            )
        )
    assert result["conversation_id"] == "conv_child"
    assert seen[0]["host_id"] == _HOST
    assert seen[0]["workspace"] == "/srv/lp"
    assert seen[0]["parent_session_id"] == "conv_parent"


async def test_config_path_mode_rejects_host_binding() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        base_url="http://server",
    ) as client:
        result = json.loads(
            await _execute_session_create(
                {"config_path": "agent.yaml", "host_id": _HOST, "workspace": "/srv/lp"},
                server_client=client,
                conversation_id="conv_parent",
                publish_event=None,
            )
        )
    assert "only with 'agent_id'" in result["error"]


async def test_schema_advertises_host_binding() -> None:
    properties = SysSessionCreateTool().get_schema()["function"]["parameters"]["properties"]
    assert properties["host_id"]["type"] == "string"
    assert properties["workspace"]["type"] == "string"
