from dataclasses import replace

import pytest

from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.sandbox import _default_sandbox_for_platform
from omnigent.server import environment_profiles
from omnigent.server.environment_profiles import validate_environment_profile_spec


def _spec_with_write_paths(write_paths: list[str]) -> OSEnvSpec:
    sandbox = replace(_default_sandbox_for_platform(), write_paths=write_paths)
    return OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=sandbox,
        fork=False,
        start_in_scratch=False,
    )


@pytest.mark.parametrize(
    ("reference", "write_paths"),
    [
        ("workspace-readonly@2", []),
        ("workspace-editable@2", ["."]),
    ],
)
def test_profile_validation_uses_published_workspace_access(
    reference: str,
    write_paths: list[str],
) -> None:
    assert validate_environment_profile_spec(reference, _spec_with_write_paths(write_paths))


def test_profile_validation_rejects_workspace_policy_mismatch() -> None:
    assert not validate_environment_profile_spec(
        "workspace-readonly@2",
        _spec_with_write_paths(["."]),
    )
    assert not validate_environment_profile_spec(
        "workspace-editable@2",
        _spec_with_write_paths([]),
    )


def test_profile_validation_rejects_unknown_reference() -> None:
    assert not validate_environment_profile_spec(
        "unknown-workspace@99",
        _spec_with_write_paths([]),
    )


def test_profile_validation_uses_catalog_policy_for_new_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = replace(
        environment_profiles.WORKSPACE_READONLY,
        id="team-workspace",
        revision=42,
    )
    monkeypatch.setitem(environment_profiles._PROFILES, profile.reference, profile)

    assert validate_environment_profile_spec(profile.reference, _spec_with_write_paths([]))
    assert not validate_environment_profile_spec(profile.reference, _spec_with_write_paths(["."]))


def test_profile_validation_rejects_a_profile_without_a_workspace_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = replace(
        environment_profiles.WORKSPACE_READONLY,
        id="unscoped-workspace",
        revision=1,
        workspace_access="unavailable",  # type: ignore[arg-type]
    )
    monkeypatch.setitem(environment_profiles._PROFILES, profile.reference, profile)

    assert not validate_environment_profile_spec(profile.reference, _spec_with_write_paths([]))
