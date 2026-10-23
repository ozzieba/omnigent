"""Resolve host maintenance authority from verified request and stored ownership."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request

from omnigent.db.db_models import current_workspace_id, uuid_to_bytes
from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC, AuthProvider
from omnigent.server.host_admission import guarded_operation
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.stores.host_admission_store import Binding, Refused
from omnigent.stores.host_store import HostStore


async def resolve_admission_binding(
    request: Request,
    *,
    auth_provider: AuthProvider | None,
    host_store: HostStore,
    host_id: str,
) -> Binding:
    """Require positive authentication and ownership in the request's workspace.

    Workspace and owner come from trusted request context, never request data.
    The admission store rechecks canonical host ownership in its transaction.
    """
    user_id = require_user(request, auth_provider)
    if (
        not isinstance(user_id, str)
        or not user_id
        or user_id
        in (
            RESERVED_USER_LOCAL,
            RESERVED_USER_PUBLIC,
        )
    ):
        raise HTTPException(status_code=401, detail="authenticated host owner required")
    workspace_id = current_workspace_id()
    try:
        selected_id = uuid_to_bytes(host_id).hex()
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="host not found") from exc
    host = await asyncio.to_thread(
        resolve_host_owner, user_id=user_id, host_id=selected_id, host_store=host_store
    )
    canonical_id = uuid_to_bytes(host.host_id).hex()
    if canonical_id != selected_id:
        raise HTTPException(status_code=409, detail="host identity changed")
    return Binding(workspace_id, canonical_id, user_id)


async def dispatch_authorized_host_operation(
    request, host_id, operation, invoke, *, is_complete=None
):
    """Guard an already-authorized operation on an explicitly enrolled host.

    Unbound execution and other entrypoints need their own integration. This
    helper alone does not qualify whole-host maintenance activation.
    """
    adapter = getattr(request.app.state, "host_admission", None)
    if adapter is None or host_id is None:
        return await invoke()
    try:
        binding = await adapter.binding_for_host(
            current_workspace_id(), uuid_to_bytes(host_id).hex()
        )
        if binding is None:
            return await invoke()
        return await guarded_operation(
            adapter, binding, operation, invoke, is_complete=is_complete
        )
    except Refused as exc:
        raise HTTPException(status_code=409, detail="host admission unavailable") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="host admission deadline exceeded") from exc
