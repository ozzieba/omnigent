# Agent environments

Omnigent environment profiles are immutable, versioned descriptions of the
resources an agent environment provides. The New Agent screen reads the
server-owned catalog before a session starts. A profile is not a general
authorization mechanism: a capability marked `requestable` does not grant
access, and credentials are never stored in a profile.

## Current profile

`workspace-readonly@1` is the only published profile. It gives the selected
agent read-only access to its session workspace through its OS environment.
It does not expose host filesystems or mounts, SSH credentials, cloud
credentials, Kubernetes, service APIs, or service indexes. Those capabilities
are listed as unavailable in the catalog and New Agent screen.

The server validates the selected profile against the exact profile revision,
the bundle's declared `environment_profile`, and the fixed read-only OS
environment policy. This prevents a bundle from selecting the profile while
requesting broader filesystem write access. The profile itself contains no
paths, tokens, or other user-specific data.

Agents can inspect the profile selected for their own session by calling
`sys_session_get_info` without a `session_id`. The result includes the immutable
profile reference and its capability manifest, including unavailable
interfaces and authorization owners. This is discovery metadata; it does not
grant capabilities or mount resources into the environment.

On each turn, Omnigent also appends a server-generated environment guide to
the agent's system instructions. It is derived from the selected profile and
is equivalent in purpose to an `AGENTS.md` environment section, but it is not
currently a separate file mounted into the workspace.

## Adding a profile revision

Add a new revision rather than changing the meaning of a published reference.
For each capability, declare its actual interfaces, access level, current
availability, authorization owner, and a reason that is useful before
launch. Only mark a capability available after the runtime provides the
listed interface and enforces its access level. Keep unavailable resources
out of the environment; catalog text alone must never create access.

For host mounts, cloud secrets, infrastructure control, and service adapters,
the implementation must bind grants to the session owner and runner, expose
least-privilege interfaces, and fail closed when a dependency is unavailable.
Prefer read-only filesystem and SQL projections for discovery. Mutating
operations should use explicit, auditable control interfaces with
resource-scoped authorization. Any secret projection must be generated for
the authorized session and must not be persisted into a shared agent bundle.

Portable environment state should be described separately from profile
identity. A profile reference says what interfaces are available; it should
not embed machine-specific mount locations or snapshots. Environment IDs and
resource handles should remain stable within a session, while mount sources,
snapshot references, and restore policy belong to session resource metadata.
