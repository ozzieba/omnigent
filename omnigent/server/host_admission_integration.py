"""Mount authenticated admission controls and own their application lifetime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from omnigent.server.auth import AuthProvider
from omnigent.server.host_admission import AsyncAdmission
from omnigent.server.routes.host_admission import create_host_admission_router
from omnigent.stores.host_store import HostStore


class HostAdmissionIntegration:
    def __init__(
        self,
        *,
        adapter: AsyncAdmission | None,
        host_store: HostStore | None,
        auth_provider: AuthProvider | None,
        maintenance_ready: Callable[[], bool] | None,
    ) -> None:
        if adapter is None:
            if maintenance_ready is not None:
                raise ValueError("host admission adapter required for maintenance readiness")
        else:
            if not isinstance(adapter, AsyncAdmission):
                raise ValueError("host admission requires the bounded async adapter")
            if host_store is None or auth_provider is None:
                raise ValueError("host admission requires host store and authentication")
            if getattr(host_store, "_engine", None) is not adapter.store.engine:
                raise ValueError("host admission and host store must share the canonical engine")
            if maintenance_ready is not None and not callable(maintenance_ready):
                raise ValueError("host maintenance readiness must be a trusted callable")
        self.adapter = adapter
        self.host_store = host_store
        self.auth_provider = auth_provider
        self.maintenance_ready = (
            maintenance_ready if maintenance_ready is not None else lambda: False
        )

    def install(self, app: FastAPI) -> None:
        if self.adapter is None:
            return
        assert self.host_store is not None
        app.state.host_admission = self.adapter
        app.include_router(
            create_host_admission_router(
                adapter=self.adapter,
                host_store=self.host_store,
                auth_provider=self.auth_provider,
                maintenance_ready=self.maintenance_ready,
            ),
            prefix="/v1",
            tags=["host-admission"],
        )

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Retain unfinished workers on close failure; caller keeps the engine.

        The adapter owns every submitted future and UNKNOWN classification.
        A failed close does not authorize engine disposal, retries or turnover.
        """
        try:
            yield
        finally:
            if self.adapter is not None:
                await self.adapter.aclose()
