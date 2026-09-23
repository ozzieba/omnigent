"""Server-owned, immutable agent environment profile catalog."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class EnvironmentProfile:
    """A published profile revision. Profiles contain no user credentials."""

    id: str
    revision: int
    name: str
    description: str
    access: str

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.revision}"

    def public_dict(self) -> dict[str, str | int]:
        return {
            "id": self.id,
            "revision": self.revision,
            "reference": self.reference,
            "name": self.name,
            "description": self.description,
            "access": self.access,
        }


# This first profile exposes only the selected workspace. Linux bwrap and
# macOS seatbelt both mount cwd read-only unless a write grant is declared.
WORKSPACE_READONLY = EnvironmentProfile(
    id="workspace-readonly",
    revision=1,
    name="Read-only workspace",
    description=(
        "Expose the session workspace to OS tools as read-only; "
        "no additional mounts or credentials."
    ),
    access="read-only",
)

_PROFILES = {WORKSPACE_READONLY.reference: WORKSPACE_READONLY}


def list_environment_profiles() -> list[dict[str, str | int]]:
    """Return the published catalog, ordered by stable reference."""
    ordered = sorted(_PROFILES.values(), key=lambda profile: profile.reference)
    return [profile.public_dict() for profile in ordered]


def get_environment_profile(reference: str) -> EnvironmentProfile | None:
    """Resolve an exact immutable ``id@revision`` reference."""
    return _PROFILES.get(reference)


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
