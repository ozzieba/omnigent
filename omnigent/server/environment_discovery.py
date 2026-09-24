"""Filesystem discovery indexes derived from server-published capabilities."""

from __future__ import annotations

from typing import Any

from omnigent.server.environment_profiles import get_environment_profile


def environment_profile_discovery_index(reference: str) -> dict[str, Any] | None:
    """Return workflows for live, permission-checked interfaces in one profile.

    This index contains metadata only: no session data, credentials, paths, or
    authorization shortcuts. Each listed tool enforces its own server policy.
    """
    profile = get_environment_profile(reference)
    if profile is None:
        return None

    capabilities = {capability.id: capability for capability in profile.capabilities}
    services: list[dict[str, Any]] = []
    sessions = capabilities.get("omnigent.sessions")
    if sessions is not None and sessions.state == "available":
        services.append(
            {
                "id": sessions.id,
                "name": sessions.name,
                "access": sessions.access,
                "authorization_owner": sessions.authorization_owner,
                "interfaces": list(sessions.interfaces),
                "workflow": [
                    "Call sys_session_list to discover sessions visible under the current session.",
                    "Call sys_session_get_info for one returned session ID.",
                    "Call sys_session_get_history only when transcript context is needed.",
                ],
            }
        )
    agents = capabilities.get("omnigent.agent-contexts")
    if agents is not None and agents.state == "available":
        services.append(
            {
                "id": agents.id,
                "name": agents.name,
                "access": agents.access,
                "authorization_owner": agents.authorization_owner,
                "interfaces": list(agents.interfaces),
                "workflow": [
                    "Call sys_agent_list to discover built-in, local, and accessible session agents.",
                    "Use sys_agent_get or sys_agent_download only with an agent ID returned by an authorized listing.",
                ],
            }
        )
    return {
        "schema_version": 1,
        "kind": "omnigent.environment-discovery-index",
        "profile": profile.reference,
        "authorization": "Metadata only; each interface enforces its own server-side permissions.",
        "services": services,
    }
