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
        "guide to OS tools as read-only; no host mounts or credentials."
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

_PROFILES = {WORKSPACE_READONLY.reference: WORKSPACE_READONLY}


def list_environment_profiles() -> list[dict[str, object]]:
    """Return the published catalog, ordered by stable reference."""
    ordered = sorted(_PROFILES.values(), key=lambda profile: profile.reference)
    return [profile.public_dict() for profile in ordered]


def get_environment_profile(reference: str) -> EnvironmentProfile | None:
    """Resolve an exact immutable ``id@revision`` reference."""
    return _PROFILES.get(reference)


def environment_profile_agent_guide(
    reference: str,
    *,
    filesystem_path: str | None = None,
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
    for capability in profile.capabilities:
        interfaces = ", ".join(capability.interfaces)
        lines.append(
            f"- **{capability.state}: {capability.name}** (`{capability.id}`); "
            f"access: {capability.access}; interfaces: {interfaces}; "
            f"authorization owner: {capability.authorization_owner}. {capability.reason}"
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
