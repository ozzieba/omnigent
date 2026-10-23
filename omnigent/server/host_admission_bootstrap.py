"""Construct the optional CLI adapter without granting maintenance readiness."""

from __future__ import annotations

from omnigent.server.auth import AuthProvider
from omnigent.server.host_admission import AsyncAdmission
from omnigent.stores.host_admission_store import Store
from omnigent.stores.host_store import HostStore


def create_cli_host_admission(
    config: object,
    *,
    host_store: HostStore,
    auth_provider: AuthProvider | None,
) -> AsyncAdmission | None:
    """Use the canonical engine; readiness remains owned by the app factory."""
    if config is None:
        return None
    if not isinstance(config, dict) or set(config) - {"enabled"}:
        raise ValueError("host_admission accepts only the enabled boolean")
    enabled = config.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("host_admission.enabled must be a boolean")
    if not enabled:
        return None
    if auth_provider is None:
        raise ValueError("host admission requires authentication")
    return AsyncAdmission(Store(host_store._engine))
