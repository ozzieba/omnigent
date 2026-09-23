"""Credential-free tool inventory bound to New Agent environment profiles."""

from omnigent.server.environment_profiles import (
    WORKSPACE_READONLY,
    environment_profile_agent_guide,
)
from omnigent.server.runtime_manifests import get_runtime_manifest


def test_profile_binds_exact_image_manifest_without_claiming_session_availability():
    profile = WORKSPACE_READONLY.public_dict()
    manifest_ref = WORKSPACE_READONLY.runtime_manifest_reference
    manifest = get_runtime_manifest(manifest_ref)

    assert manifest is not None
    assert profile["runtime_manifest"]["reference"] == manifest_ref
    assert profile["runtime_manifest"]["session_guarantee"] is False
    assert set(profile["runtime_manifest"]) == {
        "id",
        "revision",
        "reference",
        "name",
        "scope",
        "session_guarantee",
        "tools",
    }


def test_host_image_manifest_reports_declared_and_absent_tools():
    manifest = get_runtime_manifest("omnigent-host-runtime@1")

    assert manifest is not None
    tools = {tool.id: tool for tool in manifest.tools}
    assert tools["kubectl"].state == "included-in-image"
    assert "v1.36.4" in tools["kubectl"].version
    assert tools["uv, uvx"].state == "included-in-image"
    assert "resolved" in tools["uv, uvx"].version
    assert tools["gcloud"].state == "not-included"
    assert tools["nix"].state == "not-included"


def test_generated_environment_guide_discloses_image_scope_and_limits():
    guide = environment_profile_agent_guide("workspace-readonly@1")

    assert guide is not None
    assert "does not guarantee that this session uses that image" in guide
    assert "Image tool `gcloud`: not-included" in guide
    assert "Image tool `nix`: not-included" in guide
