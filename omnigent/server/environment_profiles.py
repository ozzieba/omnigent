"""Server-owned, immutable agent environment profile catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Literal


CapabilityState = Literal["available", "unavailable", "denied", "requestable"]


@dataclass(frozen=True)
class EnvironmentCapability:
    """One interface/resource fact shown to the user before launch.

    A catalog entry describes what the selected profile really supplies. It
    does not resolve paths or credentials, and a profile cannot grant itself
    access by listing a capability as requestable.
    """

    id: str
    name: str
    state: CapabilityState
    interfaces: tuple[str, ...]
    access: str
    authorization_owner: str
    reason: str

    def public_dict(self) -> dict[str, str | list[str]]:
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "interfaces": list(self.interfaces),
            "access": self.access,
            "authorization_owner": self.authorization_owner,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EnvironmentProfile:
    """A published profile revision. Profiles contain no user credentials."""

    id: str
    revision: int
    name: str
    description: str
    access: str
    capabilities: tuple[EnvironmentCapability, ...]

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.revision}"

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "revision": self.revision,
            "reference": self.reference,
            "name": self.name,
            "description": self.description,
            "access": self.access,
            "capabilities": [capability.public_dict() for capability in self.capabilities],
        }


# This first profile exposes the selected workspace and a generated,
# read-only environment guide. Linux bwrap and macOS seatbelt both mount cwd
# read-only unless a write grant is declared.
WORKSPACE_READONLY = EnvironmentProfile(
    id="workspace-readonly",
    revision=1,
    name="Read-only workspace",
    description=(
        "Expose the session workspace and a generated AGENTS.md environment "
        "guide plus a machine-readable capability catalog as read-only; "
        "no host mounts or credentials."
    ),
    access="read-only",
    capabilities=(
        EnvironmentCapability(
            id="session.workspace",
            name="Session workspace",
            state="available",
            interfaces=("shell", "filesystem-api"),
            access="read-only",
            authorization_owner="session owner",
            reason=(
                "The session workspace has OS-level read-only isolation. The generated "
                "environment guide is exposed separately under its own capability."
            ),
        ),
        EnvironmentCapability(
            id="agent.environment-guide",
            name="Environment guide",
            state="available",
            interfaces=("AGENTS.md", "sys_os_read", "shell"),
            access="read-only",
            authorization_owner="platform",
            reason=(
                "A server-generated guide is mounted from a private per-session "
                "temporary directory outside the workspace."
            ),
        ),
        EnvironmentCapability(
            id="agent.environment-catalog",
            name="Environment capability catalog",
            state="available",
            interfaces=("resources/catalog.json", "jq", "sys_session_get_info"),
            access="read-only",
            authorization_owner="platform",
            reason=(
                "The JSON file mirrors this profile's availability and authorization metadata; "
                "it contains no credentials and does not grant access to unavailable services."
            ),
        ),
        EnvironmentCapability(
            id="host.mounts",
            name="Host workspaces and mounts",
            state="unavailable",
            interfaces=("filesystem", "ssh"),
            access="none",
            authorization_owner="platform operator",
            reason="No host mounts or SSH credentials are configured in this profile.",
        ),
        EnvironmentCapability(
            id="cloud.credentials",
            name="Cloud credentials and secrets",
            state="unavailable",
            interfaces=("credential broker", "virtual files"),
            access="none",
            authorization_owner="user and platform operator",
            reason="No credential broker or per-resource grant is configured.",
        ),
        EnvironmentCapability(
            id="infrastructure.control",
            name="Kubernetes and service control",
            state="unavailable",
            interfaces=("kubectl", "service APIs"),
            access="none",
            authorization_owner="platform operator",
            reason="No cluster configuration or control-plane authorization is configured.",
        ),
        EnvironmentCapability(
            id="service.catalogs",
            name="Git, databases, chat, and service catalogs",
            state="unavailable",
            interfaces=("filesystem", "CLI", "SQLite/DuckDB"),
            access="none",
            authorization_owner="resource owner",
            reason="No service adapters, indexes, or resource grants are configured.",
        ),
    ),
)

WORKSPACE_EDITABLE = replace(
    WORKSPACE_READONLY,
    id="workspace-editable",
    name="Editable workspace",
    description=(
        "Expose the session workspace with read-write access and a generated AGENTS.md "
        "environment guide plus machine-readable capability catalog. No host mounts or credentials."
    ),
    access="read-write workspace only",
    capabilities=tuple(
        replace(
            capability,
            access="read-write",
            reason=(
                "The session workspace is the only writable host path in this profile; "
                "the OS sandbox continues to isolate all other paths."
            ),
        )
        if capability.id == "session.workspace"
        else capability
        for capability in WORKSPACE_READONLY.capabilities
    ),
)

OMNIGENT_SESSION_DISCOVERY = EnvironmentCapability(
    id="omnigent.sessions",
    name="Omnigent sessions",
    state="available",
    interfaces=("sys_session_list", "sys_session_get_info", "sys_session_get_history"),
    access="scoped read-only",
    authorization_owner="session owner and server permission policy",
    reason=(
        "These read tools are registered for every agent and return only sessions the "
        "current user is authorized to access. Spawn and session-creation tools remain "
        "separately gated by the agent spec."
    ),
)

OMNIGENT_AGENT_CONTEXTS = EnvironmentCapability(
    id="omnigent.agent-contexts",
    name="Omnigent agent contexts",
    state="available",
    interfaces=("sys_agent_list", "sys_agent_get", "sys_agent_download"),
    access="scoped read-only",
    authorization_owner="session owner and server permission policy",
    reason=(
        "Discover built-in, local, and accessible session-bound agents. Bundle reads "
        "are restricted by the server's per-user session permissions."
    ),
)

WORKSPACE_READONLY_V2 = replace(
    WORKSPACE_READONLY,
    revision=2,
    description=(
        "Expose the session workspace and generated environment guide/catalog. "
        "Omnigent session and agent-context discovery APIs are available; no host "
        "mounts or credentials are provided."
    ),
    capabilities=tuple(
        replace(
            capability,
            name="External service adapters",
            reason=(
                "No filesystem or SQL adapters are configured for Git hosting, databases, "
                "Mattermost, wikis, or other external service catalogs."
            ),
        )
        if capability.id == "service.catalogs"
        else capability
        for capability in WORKSPACE_READONLY.capabilities
    )
    + (OMNIGENT_SESSION_DISCOVERY, OMNIGENT_AGENT_CONTEXTS),
)

WORKSPACE_EDITABLE_V2 = replace(
    WORKSPACE_EDITABLE,
    revision=2,
    description=(
        "Expose the session workspace with read-write access and a generated environment "
        "guide/catalog. Omnigent session and agent-context discovery APIs are available; "
        "no host mounts or credentials are provided."
    ),
    capabilities=tuple(
        replace(
            capability,
            name="External service adapters",
            reason=(
                "No filesystem or SQL adapters are configured for Git hosting, databases, "
                "Mattermost, wikis, or other external service catalogs."
            ),
        )
        if capability.id == "service.catalogs"
        else capability
        for capability in WORKSPACE_EDITABLE.capabilities
    )
    + (OMNIGENT_SESSION_DISCOVERY, OMNIGENT_AGENT_CONTEXTS),
)

_PROFILES = {
    profile.reference: profile
    for profile in (
        WORKSPACE_READONLY,
        WORKSPACE_EDITABLE,
        WORKSPACE_READONLY_V2,
        WORKSPACE_EDITABLE_V2,
    )
}


def list_environment_profiles() -> list[dict[str, object]]:
    """Return the latest profile revision for each profile id."""
    latest: dict[str, EnvironmentProfile] = {}
    for profile in _PROFILES.values():
        current = latest.get(profile.id)
        if current is None or profile.revision > current.revision:
            latest[profile.id] = profile
    ordered = sorted(latest.values(), key=lambda profile: profile.reference)
    return [profile.public_dict() for profile in ordered]


def get_environment_profile(reference: str) -> EnvironmentProfile | None:
    """Resolve an exact immutable ``id@revision`` reference."""
    return _PROFILES.get(reference)


def environment_profile_catalog(reference: str) -> dict[str, object] | None:
    """Return the machine-readable, credential-free catalog for one profile."""
    profile = get_environment_profile(reference)
    if profile is None:
        return None
    return {
        "schema_version": 1,
        "kind": "omnigent.environment-profile",
        "profile": profile.public_dict(),
    }


def environment_profile_agent_guide(
    reference: str,
    *,
    filesystem_path: str | None = None,
    catalog_path: str | None = None,
) -> str | None:
    """Render trusted profile capability metadata for an agent system prompt.

    This is generated from the same immutable catalog shown in New Agent. It
    describes capabilities but does not grant them or reveal user credentials.
    """
    profile = get_environment_profile(reference)
    if profile is None:
        return None

    lines = [
        "## Omnigent agent environment",
        f"Profile: {profile.name} (`{profile.reference}`; {profile.access}).",
        profile.description,
        "Inspect this session's profile at any time with `sys_session_get_info`.",
        "Only capabilities marked available below are present in this environment.",
    ]
    if filesystem_path is not None:
        lines.append(
            "A read-only `AGENTS.md` copy of this environment guide is mounted at "
            f"`{filesystem_path}`."
        )
    if catalog_path is not None:
        lines.append(
            "A read-only JSON capability catalog is mounted at "
            f"`{catalog_path}`; query it with `jq` or read it with `sys_os_read`. "
            "It describes grants but does not grant them."
        )
    for capability in profile.capabilities:
        interfaces = ", ".join(capability.interfaces)
        lines.append(
            f"- **{capability.state}: {capability.name}** (`{capability.id}`); "
            f"access: {capability.access}; interfaces: {interfaces}; "
            f"authorization owner: {capability.authorization_owner}. {capability.reason}"
        )
    capability_ids = {capability.id for capability in profile.capabilities}
    if "omnigent.sessions" in capability_ids:
        lines.extend(
            [
                "",
                "### Discover Omnigent session context",
                "Use `sys_session_list` to find sessions visible to you, then "
                "`sys_session_get_info` for metadata or `sys_session_get_history` "
                "for transcript context. These tools enforce the server's session "
                "permissions; do not assume other users' sessions are visible.",
            ]
        )
    if "omnigent.agent-contexts" in capability_ids:
        lines.extend(
            [
                "",
                "### Discover Omnigent agents",
                "Use `sys_agent_list` to find built-in, local, and accessible "
                "session-bound agents. Use `sys_agent_get` or `sys_agent_download` "
                "only with a session-bound result you are allowed to access.",
            ]
        )
    return "\n".join(lines)


def validate_workspace_readonly_spec(spec: object) -> bool:
    """Accept only the fixed, cross-platform read-only workspace policy."""
    from omnigent.inner.datamodel import OSEnvSpec
    from omnigent.inner.sandbox import _default_sandbox_for_platform

    if not isinstance(spec, OSEnvSpec):
        return False
    default_sandbox = _default_sandbox_for_platform()
    expected_sandbox = replace(default_sandbox, write_paths=[])
    expected = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=expected_sandbox,
        fork=False,
        start_in_scratch=False,
    )
    return asdict(spec) == asdict(expected)


def validate_environment_profile_spec(reference: str, spec: object) -> bool:
    """Accept only the exact OS sandbox policy published for a profile."""
    from omnigent.inner.datamodel import OSEnvSpec
    from omnigent.inner.sandbox import _default_sandbox_for_platform

    if not isinstance(spec, OSEnvSpec):
        return False
    if reference in (WORKSPACE_READONLY.reference, WORKSPACE_READONLY_V2.reference):
        expected_sandbox = replace(_default_sandbox_for_platform(), write_paths=[])
    elif reference in (WORKSPACE_EDITABLE.reference, WORKSPACE_EDITABLE_V2.reference):
        expected_sandbox = replace(_default_sandbox_for_platform(), write_paths=["."])
    else:
        return False
    expected = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=expected_sandbox,
        fork=False,
        start_in_scratch=False,
    )
    return asdict(spec) == asdict(expected)
