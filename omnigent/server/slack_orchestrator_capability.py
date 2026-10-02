"""Narrow OAuth capability used by the Slack orchestrator gateway."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from fastapi import HTTPException, Request

CLIENT_ID = "slack-orchestrator-gateway"
SCOPE = "slack-orchestrator"
POLICY_FILE_ENV = "OMNIGENT_SLACK_ORCHESTRATOR_CAPABILITY_FILE"
GRANT_LABEL = "slack_orch.grant_id"
SESSION = re.compile(r"[0-9a-f]{32}")
REGISTRY = re.compile(r"orch_[0-9a-f]{24}")
NAME = re.compile(r"[a-z][a-z0-9-]{2,95}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _deny(detail: str) -> NoReturn:
    raise HTTPException(status_code=403, detail=detail)


@dataclass(frozen=True)
class CreateTemplate:
    key: str
    family: str
    name_prefix: str
    agent_id: str
    model_override: str
    harness_override: str
    host_id: str
    workspace: str | None = None
    reasoning_effort: str | None = None

    def fixed_body(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "agent_id": self.agent_id,
            "model_override": self.model_override,
            "harness_override": self.harness_override,
            "host_id": self.host_id,
        }
        if self.workspace is not None:
            result["workspace"] = self.workspace
        if self.reasoning_effort is not None:
            result["reasoning_effort"] = self.reasoning_effort
        return result


@dataclass(frozen=True)
class CapabilityPolicy:
    subject: str
    approver_subject: str
    catalog_sha256: str
    seed_sessions: frozenset[str]
    templates: tuple[CreateTemplate, ...]

    @classmethod
    def from_json(cls, value: str) -> CapabilityPolicy:
        raw = json.loads(value)
        policy = cls(
            subject=str(raw["subject"]),
            approver_subject=str(raw["approver_subject"]),
            catalog_sha256=str(raw["catalog_sha256"]),
            seed_sessions=frozenset(str(item) for item in raw["seed_sessions"]),
            templates=tuple(CreateTemplate(**item) for item in raw["templates"]),
        )
        policy.validate()
        return policy

    def canonical_json(self) -> str:
        value = {
            "subject": self.subject,
            "approver_subject": self.approver_subject,
            "catalog_sha256": self.catalog_sha256,
            "seed_sessions": sorted(self.seed_sessions),
            "templates": [template.__dict__ for template in self.templates],
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def validate(self) -> None:
        if self.subject != "svc-slack-orchestrator-gateway" or not self.approver_subject:
            raise ValueError("invalid capability identities")
        if not SHA256.fullmatch(self.catalog_sha256):
            raise ValueError("invalid catalog digest")
        if not self.seed_sessions or any(
            not SESSION.fullmatch(item) for item in self.seed_sessions
        ):
            raise ValueError("invalid seed sessions")
        if not self.templates or len({item.key for item in self.templates}) != len(self.templates):
            raise ValueError("invalid templates")
        for item in self.templates:
            if (
                not NAME.fullmatch(item.name_prefix)
                or not item.name_prefix.endswith("-orch")
                or not all(
                    (
                        item.key,
                        item.family,
                        item.agent_id,
                        item.model_override,
                        item.harness_override,
                        item.host_id,
                    )
                )
            ):
                raise ValueError("invalid template")


def policy_for_device_client(client_id: str | None) -> str | None:
    """Return canonical policy JSON for the reserved client, or None otherwise."""
    if client_id != CLIENT_ID:
        return None
    path = os.environ.get(POLICY_FILE_ENV, "").strip()
    if not path:
        raise ValueError(f"{POLICY_FILE_ENV} is required for {CLIENT_ID}")
    raw = Path(path).read_text(encoding="utf-8")
    policy = CapabilityPolicy.from_json(raw)
    canonical = policy.canonical_json()
    # The configured catalog digest is independently reviewed; recording a
    # canonical policy prevents formatting differences from changing grants.
    hashlib.sha256(canonical.encode()).hexdigest()
    return canonical


def _raw_path(request: Request) -> str:
    value = request.scope.get("raw_path", request.url.path.encode("ascii", "strict"))
    path: str
    try:
        path = bytes(value).decode("ascii")
    except (UnicodeDecodeError, TypeError, ValueError):
        _deny("non-canonical capability path")
    if (
        "%" in path
        or "\\" in path
        or "//" in path
        or path.endswith("/")
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        _deny("non-canonical capability path")
    return path


def _query(request: Request) -> dict[str, str]:
    pairs = list(request.query_params.multi_items())
    if len({key for key, _ in pairs}) != len(pairs):
        _deny("duplicate capability query key")
    return dict(pairs)


def _raw_query(request: Request) -> bytes:
    value = request.scope.get("query_string", b"")
    return bytes(value) if isinstance(value, (bytes, bytearray)) else b""


def path_allowed(method: str, path: str) -> bool:
    """Coarse auth-layer boundary; route handlers enforce exact shapes."""
    if method == "GET" and path == "/v1/sessions":
        return True
    if method == "POST" and path == "/v1/sessions":
        return True
    match = re.fullmatch(r"/v1/sessions/([0-9a-f]{32})/(events|items)", path)
    return bool(match and ((method == "POST") == (match.group(2) == "events")))


async def request_policy(request: Request, permission_store: Any) -> CapabilityPolicy | None:
    """Load and validate the live grant policy for a capability token."""
    if getattr(request.state, "oauth_scope", None) != SCOPE:
        return None
    grant_id = getattr(request.state, "oauth_grant_id", None)
    subject = getattr(request.state, "oauth_subject", None)
    client_id = getattr(request.state, "oauth_client_id", None)
    jti = getattr(request.state, "oauth_jti", None)
    store = getattr(request.app.state, "device_grant_store", None)
    if (
        not isinstance(grant_id, str)
        or not isinstance(jti, str)
        or not jti
        or store is None
        or client_id != CLIENT_ID
    ):
        _deny("capability context missing")
    raw = await asyncio.to_thread(store.get_capability_json, grant_id)
    if not isinstance(raw, str):
        _deny("capability grant missing")
    try:
        policy = CapabilityPolicy.from_json(raw)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _deny("capability grant invalid")
    if subject != policy.subject or permission_store is None:
        _deny("capability subject mismatch")
    if await asyncio.to_thread(permission_store.is_admin, subject):
        _deny("capability subject must remain non-admin")
    return policy


async def permitted_sessions(request: Request, policy: CapabilityPolicy) -> frozenset[str]:
    store = request.app.state.device_grant_store
    grant_id = request.state.oauth_grant_id
    created = await asyncio.to_thread(store.list_capability_sessions, grant_id)
    return policy.seed_sessions | frozenset(created)


async def register_session(request: Request, session_id: str) -> None:
    """Persist a grant/session binding after a policy-authorized create."""
    await asyncio.to_thread(
        request.app.state.device_grant_store.register_capability_session,
        request.state.oauth_grant_id,
        session_id,
    )


def authorize_list(request: Request) -> None:
    if request.method != "GET" or _raw_path(request) != "/v1/sessions":
        _deny("capability operation denied")
    if (
        _query(request) != {"limit": "1000", "include_archived": "false"}
        or _raw_query(request) != b"limit=1000&include_archived=false"
    ):
        _deny("capability list query denied")


def authorize_items(request: Request, session_id: str, allowed: frozenset[str]) -> None:
    if (
        request.method != "GET"
        or _raw_path(request) != f"/v1/sessions/{session_id}/items"
        or session_id not in allowed
        or _query(request) != {"limit": "50", "order": "asc"}
        or _raw_query(request) != b"limit=50&order=asc"
    ):
        _deny("capability items request denied")


def authorize_event(request: Request, session_id: str, allowed: frozenset[str], body: Any) -> None:
    if (
        request.method != "POST"
        or _raw_path(request) != f"/v1/sessions/{session_id}/events"
        or session_id not in allowed
        or _query(request)
    ):
        _deny("capability event request denied")
    if (
        not isinstance(body, Mapping)
        or set(body) != {"type", "data"}
        or body.get("type") != "message"
    ):
        _deny("only message events are permitted")
    data = body.get("data")
    if (
        not isinstance(data, Mapping)
        or set(data) != {"role", "content"}
        or data.get("role") != "user"
    ):
        _deny("capability message shape denied")
    content = data.get("content")
    if not isinstance(content, list) or len(content) != 1:
        _deny("one text part is required")
    part = content[0]
    if (
        not isinstance(part, Mapping)
        or set(part) != {"type", "text"}
        or part.get("type") != "input_text"
    ):
        _deny("capability message part denied")
    text = part.get("text")
    if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= 8192:
        _deny("capability message size denied")


def deny_bundled_create(request: Request) -> None:
    """Refuse the multipart bundled create to a capability token.

    That branch of ``POST /v1/sessions`` stores a caller-supplied agent
    bundle, so it can never match a reviewed template. The auth layer cannot
    tell it from the JSON create (same method and path), so the route must.
    """
    if getattr(request.state, "oauth_scope", None) == SCOPE:
        _deny("capability create request denied")


def authorize_create(request: Request, policy: CapabilityPolicy, body: Any) -> None:
    if request.method != "POST" or _raw_path(request) != "/v1/sessions" or _query(request):
        _deny("capability create request denied")
    if not isinstance(body, Mapping):
        _deny("capability create body denied")
    for template in policy.templates:
        fixed = template.fixed_body()
        if any(body.get(key) != value for key, value in fixed.items()):
            continue
        if set(body) != set(fixed) | {"title", "labels"}:
            continue
        title, labels = body.get("title"), body.get("labels")
        if (
            not isinstance(title, str)
            or not NAME.fullmatch(title)
            or not title.startswith(template.name_prefix + "-")
            or not isinstance(labels, Mapping)
        ):
            continue
        keys = {
            "slack_orch.registry_id",
            "slack_orch.type",
            "slack_orch.family",
            "slack_orch.name",
            "slack_orch.catalog_sha256",
        }
        if set(labels) != keys:
            continue
        if (
            REGISTRY.fullmatch(str(labels["slack_orch.registry_id"]))
            and labels["slack_orch.type"] == template.key
            and labels["slack_orch.family"] == template.family
            and labels["slack_orch.name"] == title
            and labels["slack_orch.catalog_sha256"] == policy.catalog_sha256
        ):
            return
    _deny("create body does not match a reviewed template")
