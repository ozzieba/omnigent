# Agent environments

Omnigent environment profiles are immutable, versioned descriptions of the
resources an agent environment provides. The New Agent screen reads the
server-owned catalog before a session starts. A profile is not a general
authorization mechanism: a capability marked `requestable` does not grant
access, and credentials are never stored in a profile.

## Current profiles

The server currently publishes two profile IDs, each with immutable revisions:

| Profile | Current revision | Workspace access | Additional available interfaces |
| --- | --- | --- | --- |
| `workspace-readonly` | `@2` | Read-only | Omnigent session and agent-context discovery |
| `workspace-editable` | `@2` | Read-write | Omnigent session and agent-context discovery |

Both revisions expose the generated environment `AGENTS.md` guide and a
machine-readable `resources/catalog.json` through the OS environment. Revision
`@1` remains resolvable for sessions and bundles that selected it; new-agent
catalog listing returns the latest revision for each ID. The catalog is a
snapshot of capability availability and authorization metadata. It contains no
credentials and does not create access to unavailable services. Neither
profile exposes host filesystems or mounts, SSH credentials, cloud credentials,
Kubernetes, external service APIs, or external service indexes; those
capabilities are listed as unavailable in the catalog and New Agent screen.

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

Both current `@2` profiles also list `omnigent.sessions` and
`omnigent.agent-contexts` as available scoped read-only capabilities. Their
interfaces are `sys_session_list`, `sys_session_get_info`,
`sys_session_get_history`, `sys_agent_list`, `sys_agent_get`, and
`sys_agent_download`; server-side user and session permissions still constrain
every result. Child sessions inherit the selected immutable profile reference
from trusted parent session metadata.

On each turn, Omnigent appends a server-generated environment guide to the
agent's system instructions. The runner stores the same guide as a session
scoped `AGENTS.md` and writes the catalog as `resources/catalog.json` in a
private temporary directory. It adds that directory as a read-only sandbox
grant. Both exact paths are included in the guide for `sys_os_read` and shell
access. These generated files are outside the user's workspace and are
recreated when the runner reinitializes the session; they are not portable
workspace state.

The default Omnigent managed-host container includes `uv`/`uvx` for isolated
Python tooling, `jq` for JSON catalogs, and `sqlite3` for local structured data.
Custom runner images must provide their own equivalent tools; the profile
catalog does not imply that a particular binary is installed. `sys_os_read`
remains the portable way to inspect the generated catalog.

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
Prefer read-only filesystem and SQL projections for discovery. A filesystem
view should preserve familiar CLI navigation; SQLite or DuckDB tables should
cover structured metadata, joins, and filtering; neither view should bypass the
same authorization checks as its source adapter. Mutating operations should use
explicit, auditable control interfaces with resource-scoped authorization. Any
secret projection must be generated for the authorized session, be read-only
unless a narrowly scoped write is required, and must not be persisted into a
shared agent bundle.

Portable environment state should be described separately from profile
identity. A profile reference says what interfaces are available; it should
not embed machine-specific mount locations or snapshots. Environment IDs and
resource handles should remain stable within a session, while mount sources,
snapshot references, and restore policy belong to session resource metadata.
Keep portable workspace state distinct from live integrations: sockets, device
handles, credentials, network routes, and host-backed mounts need to be
rebound or reauthorized on restore. A snapshot or serialized manifest must
record those dependencies and report which could not be restored instead of
implying that a full process or VM snapshot made them portable.

## Environment work still required

The present catalog is a discoverability and scoping foundation; it is not yet
the requested shared sandbox. The main implementation tracks are:

1. **Agent runtime and packages:** stable, versioned toolchains (including
   `kubectl`, `gcloud`, `nix`, `uv`, and optimized search tools), with explicit
   install authority and portable package/environment manifests.
2. **Host and cluster access:** HA Kubernetes endpoint and scoped kubeconfig,
   SSH host aliases, and selected host/storage mounts. Mounts must identify
   their owning host and their failover or degraded behavior.
3. **Service adapters:** read-only file views and structured tables for Git,
   Forgejo Actions, Mattermost, wiki, PostgreSQL, Omnigent sessions/contexts,
   Terraform, Kubernetes objects, browsers, devices, VMs, containers, and
   mounted storage. Add write/control interfaces as separately authorized
   operations.
4. **Credentials:** per-session Secret Manager projections and generated
   provider dotfiles with explicit resource scopes, redaction, expiry, and
   cleanup; never copy secret values into the agent bundle or shared snapshot.
5. **Portable environments:** stable environment IDs plus a manifest of image,
   package lock, workspace snapshot, mount sources, and restore requirements.
   VM snapshots may accelerate restore, but must not replace recording external
   dependencies or authorization state.
6. **Service HA and discovery:** move remaining r820-owned services into
   Kubernetes only after their data owner, persistent state, and write path are
   reconciled. Give each workload a health-based endpoint and documented
   failure domain; publish its agent-facing adapter only after endpoint and
   failover behavior are verified.

The New Agent control should select a published, server-enforced profile. As
resource adapters become available, expose their availability, access level,
authorization owner, and interfaces there before launch. A UI checkbox or
catalog entry by itself is never an authorization grant.
