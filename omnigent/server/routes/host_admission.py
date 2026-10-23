"""Authenticated maintenance controls for explicitly enrolled host scopes."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.exc import IntegrityError

from omnigent.server.auth import AuthProvider
from omnigent.server.host_admission import AsyncAdmission
from omnigent.server.routes._host_admission import resolve_admission_binding
from omnigent.stores.host_admission_store import Refused
from omnigent.stores.host_store import HostStore


class DrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_generation: StrictInt = Field(ge=0)


def create_host_admission_router(
    *,
    adapter: AsyncAdmission,
    auth_provider: AuthProvider | None,
    host_store: HostStore,
    maintenance_ready: Callable[[], bool],
) -> APIRouter:
    """Require server-owned coverage readiness before any enrollment or drain.

    The caller must qualify all entrypoints and replicas before readiness.
    Request data cannot enable it. No release-by-hash endpoint is exposed.
    """
    router = APIRouter()

    async def binding(request, host_id):
        return await resolve_admission_binding(
            request, auth_provider=auth_provider, host_store=host_store, host_id=host_id
        )

    def require_ready():
        if maintenance_ready() is not True:
            raise HTTPException(status_code=503, detail="host maintenance coverage unqualified")

    @router.post("/hosts/{host_id}/admission", status_code=201)
    async def enroll(request: Request, host_id: str):
        scope = await binding(request, host_id)
        require_ready()
        try:
            await adapter.enroll(scope)
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="host already enrolled") from exc
        except Refused as exc:
            raise HTTPException(status_code=409, detail="host ownership changed") from exc
        return {"enrolled": True, "runtime_quiet_proved": False}

    @router.get("/hosts/{host_id}/admission")
    async def status(request: Request, host_id: str):
        scope = await binding(request, host_id)
        try:
            return await adapter.status(scope)
        except Refused as exc:
            raise HTTPException(status_code=409, detail="host admission unavailable") from exc

    @router.post("/hosts/{host_id}/maintenance")
    async def drain(request: Request, host_id: str, body: DrainRequest):
        scope = await binding(request, host_id)
        require_ready()
        try:
            fence = await adapter.drain(scope, body.expected_generation)
        except Refused as exc:
            raise HTTPException(status_code=409, detail="host admission changed") from exc
        return {
            "generation": fence.generation,
            "fence_token": fence.token,
            "admission_held": True,
            "runtime_quiet_proved": False,
        }

    return router
