# Captured build receipts for long-lived runners

Status: bounded prototype; no deployment or runtime replacement is included.

## Problem and existing behavior

Replacing package files does not replace Python objects retained by a running
runner or zygote. An installed-file checksum or a successful import in a new
interpreter therefore cannot establish which build an existing process uses.
A new child PID also does not establish a cold import: a fork can inherit an
older graph.

The current source already protects zygote forks with `_GraphStamp`: it records
the build stamp and package-wide Python source metadata before preloading the
runner graph, then rejects forks after those files change. Preserve that guard.
It addresses mixed-version forks, but does not expose a retained process receipt
or replace existing long-lived runners. Earlier deployed versions may have a
different guard; the motivating incident is not evidence that current source
lacks source invalidation.

That guard compares metadata, not source content. Immediate same-size rewrites
can retain modification time and inode on a filesystem; those changes are not
detected. Prototype validation reproduced this behavior and the corresponding
existing guard-test failures on the unchanged source baseline. Immutable release
directories avoid relying on this metadata shortcut for artifact identity. The
prototype neither repairs that separate limitation nor weakens the guard.

`setup.py` currently generates `_build_info.py` with `COMMIT_SHA` and
`BUILD_TIME_EPOCH`. A source checkout or a build without Git can lack a usable
stamp. There is no wheel digest in that module, and a dirty tree or later manual
patch can share its commit and timestamp with different source bytes.

## Three distinct pieces of evidence

| Evidence | Producer and time | What it establishes |
| --- | --- | --- |
| Intended artifact | Reviewed build output and manifest | The release the owner authorized |
| Installed artifact | Adoption verifier, against the selected release directory | The artifact currently available to fresh processes |
| Captured build stamp | Runtime module import, retained in memory | The stamp this import graph captured, including inheritance across fork |

Never substitute one for another. In particular, **a captured build stamp is
not an attestation of all loaded code**. A matching commit and build timestamp
cannot prove manual patches, dependency versions, later lazy imports, wheel
identity, or complete source equivalence. An unknown or matching stamp is not
permission to claim runtime acceptance.

## Prototype contract

`omnigent.build_receipt` captures its immutable snapshot once on module import.
The runner entrypoint and zygote explicitly import it before the runner graph.
The snapshot stays in the canonical module even when the entrypoint runs as
`__main__`. Forked children inherit it; no at-fork refresh is registered.

Each runner hello includes an optional `captured_build` object. Reconnects reuse
the retained snapshot. The existing authenticated
`GET /v1/runners/{runner_id}/status` returns it only for a visible, online runner,
using the stored hello without reading disk or inspecting the process.

| Field | Meaning |
| --- | --- |
| `schema_version` | `1`; unknown versions are ignored |
| `commit_sha`, `build_time_epoch` | Captured generated metadata; both null if unavailable or invalid |
| `captured_at_epoch` | Snapshot time, **not** OS process start time |
| `capture_pid` | Process that captured the snapshot |
| `process_pid` | Process reporting the receipt |
| `load_mode` | `process-import` when those PIDs match; `inherited` otherwise |

PIDs are local to the runner's process namespace and can be reused. Interpret
them together with the existing runner binding and capture timestamp, not as
global process identifiers. `inherited` states an observed fork relationship;
it does not prove the named ancestor is still alive. No hostnames, filesystem
paths, process environment, credentials, prompts, or logs are included.

The receipt is self-reported operational metadata, not a trust or authorization
mechanism. Its decoder accepts only the defined typed fields and drops unknown
keys. Malformed metadata cannot prevent an otherwise valid legacy handshake.
Old runners omit it; old servers ignore the additional hello field. The wire
protocol major, existing status keys, owner filtering, and offline behavior do
not change. Offline receipts are not persisted by this prototype.

## Adoption and artifact manifest follow-up

Keep the existing wheel/venv workflow. The next artifact change should produce
an external manifest containing the source commit, ordered reviewed patch
identities, source content manifest digest, dependency lock digest, Python/ABI,
build inputs and command, and final wheel SHA-256. Build into an immutable
release directory and retain the previous directory for owner-driven rollback.

A wheel cannot contain its own final digest. An embedded content-manifest ID
can identify pre-wheel source/build inputs; the external manifest then maps it
to the finished wheel digest. That requires a separate build-pipeline change
and integrity checks, not an extra field populated from whatever is on disk
when status is requested.

An adoption verifier should report independent outcomes:

1. Intended versus installed artifact digest: match, mismatch, or unknown.
2. Captured stamp versus expected build metadata: match, mismatch, or unknown.
3. Process provenance: imported here or inherited; retain the original capture
   time even when the child process is new.
4. Actual behavior acceptance through the normal qualified client path.

A captured-stamp mismatch detects stale runtime evidence. A stamp match alone
is only a metadata match. In-place edits with an unchanged build stamp remain
inconclusive, including when the installed verifier finds the expected bytes.
Do not refresh an old process's receipt to make those checks agree. Runtime
replacement remains an explicit action of the existing host owner.

## Validation and limits

The regression runs an isolated Python package, captures build A, replaces the
on-disk metadata with build B, and verifies the retained process still reports
A while a fresh interpreter reports B. The fork variant performs the same disk
replacement and confirms a fresh child PID retains A and its ancestor's capture
PID/time. These are small metadata subprocesses with no harness or model calls.

Additional checks cover missing/malformed build metadata, typed frame round
trips, legacy hello compatibility, reconnect without recapture, production
hello-to-status transport, owner filtering, and offline omission. Existing
zygote guard tests remain relevant and unchanged in behavior; their same-size
rewrite case has the baseline filesystem limitation described above.

This prototype does not build wheels, enforce immutable deployment, hash all
loaded code, change the host daemon API, or publish SDK/UI conveniences. It
cannot detect an unstamped patch solely from the new receipt.

The happy-path e2e packages the real Python modules into temporary source
archives A/B and installs them into an isolated directory. A fixture-managed
runner subprocess imports the production entrypoint and serves the production
tunnel. The production server route receives its hello over a real loopback
WebSocket and exposes the receipt over HTTP. After installing B, A's running
process keeps its receipt; a fresh child reports B. No session, native harness,
provider, MCP client or real host daemon is started, and subprocesses receive
an isolated allowlisted environment. This covers the artifact-to-process-to-
status metadata path, not wheel construction or fleet supervision. Actual
wheel/manifest integration and owner-qualified fleet adoption remain separate.

To reproduce the bounded checks in an isolated development environment:

```sh
uv run --no-sync pytest -q tests/runner/test_build_receipt.py tests/runner/transports/ws_tunnel/test_frames.py tests/runner/transports/ws_tunnel/test_serve.py tests/server/integration/test_runner_tunnel_route.py tests/host/test_runner_zygote.py
uv run --no-sync pytest -q tests/e2e/test_captured_build_receipt_e2e.py
uv run --no-sync pyrefly check
```

After owner-qualified adoption, request status for a known owned runner. Record
`captured_build` before and after deploying a new immutable artifact without
replacing that runner. The old receipt must remain old; a qualified cold
replacement should report the new metadata. Do not perform this scenario by
editing a live shared installation.

## Build approach evaluation

The demonstrated gap is separating retained process state from installed
files. A new build system does not update objects already loaded by a process.
This prototype needs no new dependencies, Nix, Bazel, or dependency migration.

If a later controlled experiment compares build approaches, use the same
source, patch set, dependency pins and output checks. Record clean and cached
build time, whether two clean artifact digests match (including timestamp
effects), manifest completeness, maintenance/setup cost, and rollback steps.
No such timings or reproducibility results have been measured here. Start with
the current wheel workflow plus a manifest; adopt a different approach only if
the measured reduction in drift justifies its maintenance cost.
