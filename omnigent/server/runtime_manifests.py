"""Versioned, credential-free manifests for Omnigent runner images.

Image contents are not session grants. The manifest records build declarations
so the New Agent catalog can disclose them without claiming that a selected
session has that image or access to its tools.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeTool:
    id: str
    state: str
    version: str
    source: str
    reason: str

    def public_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "state": self.state,
            "version": self.version,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RuntimeManifest:
    id: str
    revision: int
    name: str
    scope: str
    session_guarantee: bool
    tools: tuple[RuntimeTool, ...]

    @property
    def reference(self) -> str:
        return f"{self.id}@{self.revision}"

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "revision": self.revision,
            "reference": self.reference,
            "name": self.name,
            "scope": self.scope,
            "session_guarantee": self.session_guarantee,
            "tools": [tool.public_dict() for tool in self.tools],
        }


# Kept in sync with the `host` target of deploy/docker/Dockerfile. Version
# declarations are explicit so the UI does not accidentally imply that all
# tools are pinned or present in every runner.
OMNIGENT_HOST_RUNTIME = RuntimeManifest(
    id="omnigent-host-runtime",
    revision=1,
    name="Omnigent host image toolchain",
    scope="declared contents of deploy/docker/Dockerfile:host",
    session_guarantee=False,
    tools=(
        RuntimeTool(
            id="kubectl",
            state="included-in-image",
            version="v1.36.4 (pinned; checksum verified)",
            source="deploy/docker/Dockerfile",
            reason=(
                "Installed in the host image; this does not configure a kubeconfig or "
                "grant cluster access."
            ),
        ),
        RuntimeTool(
            id="uv, uvx",
            state="included-in-image",
            version="resolved by builder dependency installation",
            source="builder stage copied into host image",
            reason=(
                "The image copies these binaries but does not pin their release in the Dockerfile."
            ),
        ),
        RuntimeTool(
            id="gcloud",
            state="not-included",
            version="",
            source="deploy/docker/Dockerfile:host",
            reason="No Cloud SDK or credential broker is configured by this image.",
        ),
        RuntimeTool(
            id="nix",
            state="not-included",
            version="",
            source="deploy/docker/Dockerfile:host",
            reason="Nix is not installed by this image.",
        ),
    ),
)

_MANIFESTS = {OMNIGENT_HOST_RUNTIME.reference: OMNIGENT_HOST_RUNTIME}


def get_runtime_manifest(reference: str) -> RuntimeManifest | None:
    """Resolve an exact immutable runtime manifest revision."""
    return _MANIFESTS.get(reference)
