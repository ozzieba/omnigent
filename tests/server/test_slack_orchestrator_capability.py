"""Security contract for the Slack orchestrator OAuth capability."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from omnigent.server.slack_orchestrator_capability import (
    CapabilityPolicy,
    authorize_create,
    authorize_event,
    authorize_items,
    authorize_list,
    path_allowed,
)

SEED = "a" * 32
DIGEST = "c" * 64
POLICY = CapabilityPolicy.from_json(
    """{
      "subject":"svc-slack-orchestrator-gateway",
      "approver_subject":"admin",
      "catalog_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "seed_sessions":["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
      "templates":[{
        "key":"sol","family":"codex","name_prefix":"sol-orch",
        "agent_id":"agent-sol","model_override":"gpt-6.1-sol",
        "harness_override":"codex-native","host_id":"host-1",
        "workspace":"/home/oz","reasoning_effort":"medium"
      }]
    }"""
)


def _request(method: str, path: str, query: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [],
            "server": ("test", 443),
            "client": ("test", 1),
            "scheme": "https",
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )


def _message(text: str = "hello") -> dict[str, object]:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def _create() -> dict[str, object]:
    return {
        "agent_id": "agent-sol",
        "model_override": "gpt-6.1-sol",
        "harness_override": "codex-native",
        "host_id": "host-1",
        "workspace": "/home/oz",
        "reasoning_effort": "medium",
        "title": "sol-orch-calm-otter",
        "labels": {
            "slack_orch.registry_id": "orch_" + "d" * 24,
            "slack_orch.type": "sol",
            "slack_orch.family": "codex",
            "slack_orch.name": "sol-orch-calm-otter",
            "slack_orch.catalog_sha256": DIGEST,
        },
    }


def test_auth_layer_allows_only_four_operation_shapes() -> None:
    assert path_allowed("GET", "/v1/sessions")
    assert path_allowed("POST", "/v1/sessions")
    assert path_allowed("GET", f"/v1/sessions/{SEED}/items")
    assert path_allowed("POST", f"/v1/sessions/{SEED}/events")
    for method, path in (
        ("GET", "/v1/agents"),
        ("DELETE", f"/v1/sessions/{SEED}"),
        ("POST", f"/v1/sessions/{SEED}/items"),
        ("GET", f"/v1/sessions/{SEED}/events"),
        ("GET", "/v1/sessions/"),
    ):
        assert not path_allowed(method, path)


def test_exact_queries_and_session_scope() -> None:
    authorize_list(_request("GET", "/v1/sessions", "limit=1000&include_archived=false"))
    authorize_items(
        _request("GET", f"/v1/sessions/{SEED}/items", "limit=50&order=asc"),
        SEED,
        frozenset({SEED}),
    )
    with pytest.raises(HTTPException):
        authorize_list(_request("GET", "/v1/sessions", "limit=1000&limit=10"))
    with pytest.raises(HTTPException):
        authorize_list(_request("GET", "/v1/sessions", "include_archived=false&limit=1000"))
    with pytest.raises(HTTPException):
        authorize_items(
            _request("GET", f"/v1/sessions/{'b' * 32}/items", "limit=50&order=asc"),
            "b" * 32,
            frozenset({SEED}),
        )


def test_only_bounded_user_message_is_accepted() -> None:
    authorize_event(
        _request("POST", f"/v1/sessions/{SEED}/events"),
        SEED,
        frozenset({SEED}),
        _message(),
    )
    bad = _message()
    bad["created_by"] = "admin"
    with pytest.raises(HTTPException):
        authorize_event(
            _request("POST", f"/v1/sessions/{SEED}/events"),
            SEED,
            frozenset({SEED}),
            bad,
        )
    with pytest.raises(HTTPException):
        authorize_event(
            _request("POST", f"/v1/sessions/{SEED}/events"),
            SEED,
            frozenset({SEED}),
            _message("x" * 8193),
        )


def test_create_matches_reviewed_template_without_field_injection() -> None:
    authorize_create(_request("POST", "/v1/sessions"), POLICY, _create())
    bad = _create()
    bad["permissions"] = ["admin"]
    with pytest.raises(HTTPException):
        authorize_create(_request("POST", "/v1/sessions"), POLICY, bad)
    bad = _create()
    bad["model_override"] = "other"
    with pytest.raises(HTTPException):
        authorize_create(_request("POST", "/v1/sessions"), POLICY, bad)
