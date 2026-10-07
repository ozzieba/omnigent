"""Proxy the burst slider between the omnigent web UI and llmq.

The UI authenticates to THIS server (cookie); this router forwards burst
changes to llmq using the omnigent policy token (never exposed to the
browser). The multiplier widens llmq's instant-dispatch bucket only; the
sustained rate is untouched.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

_LLMQ_ORIGIN = "http://100.87.142.18:17645"
_POLICY_TOKEN = Path("/etc/llmq/omnigent-policy.token")
_DEFAULT_ACTOR = "r820-gateway-omnigent-personal"


def _policy_token() -> str:
    try:
        token = _POLICY_TOKEN.read_text().strip()
    except OSError as exc:
        raise HTTPException(503, "llmq policy token unavailable") from exc
    return token or (_ for _ in ()).throw(HTTPException(503, "llmq policy token empty"))


def _llmq(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        _LLMQ_ORIGIN + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + _policy_token(),
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise HTTPException(exc.code, "llmq rejected the burst update") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise HTTPException(502, "llmq unreachable") from None
    return json.loads(payload) if payload else None


def create_llmq_burst_router(*, auth_provider: AuthProvider | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/llmq/burst")
    async def get_burst(
        request: Request, workstream: str, actor: str | None = None
    ) -> dict[str, Any]:
        require_user(request, auth_provider)
        query = "?workstream=" + urllib.parse.quote(workstream)
        if actor:
            query += "&actor=" + urllib.parse.quote(actor)
        result = _llmq("GET", "/v1/burst" + query) or {}
        return {"multiplier": result.get("multiplier", 1.0)}

    @router.put("/llmq/burst")
    async def put_burst(
        request: Request, workstream: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        require_user(request, auth_provider)
        multiplier = body.get("multiplier")
        if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool):
            raise HTTPException(422, "multiplier must be a number")
        if not 0.1 <= float(multiplier) <= 10.0:
            raise HTTPException(422, "multiplier must be within [0.1, 10]")
        actor = body.get("actor") or _DEFAULT_ACTOR
        _llmq(
            "PUT",
            "/v1/burst?workstream=" + urllib.parse.quote(workstream),
            {"multiplier": float(multiplier), "actor": actor},
        )
        return {
            "ok": True,
            "workstream": workstream,
            "actor": actor,
            "multiplier": float(multiplier),
        }

    return router
