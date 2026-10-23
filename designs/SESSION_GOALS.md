# Durable goals for Claude and OpenCode

Claude/OpenCode native sessions support `GET` and `PUT
/v1/sessions/{session_id}/goals`. This is an Omnigent-owned goal, not a claim
that either vendor provides Codex's `thread/goal` protocol. Codex remains on
`codex_goal` without a second competing goal source.

Goals reside in the tenant-scoped `session_goals` table in the conversation
DB, independently of transcript items, runner availability and compaction.
There is one current goal per session. A fork does not copy it; deleting the
conversation subtree cleans up its goals. Session switches retain the record,
but only the supported native wrappers deliver it.

A write needs the existing session edit permission and `expected_revision`
(zero for a missing record). Concurrent/stale writes return 409, rather than
overwriting another owner. Reads require session read permission. No new
credential, identity bypass or privilege is introduced.

Example body:

```json
{
  "objective": "Restore useful data, retain originals, verify the same snapshot's hashes. Next: inspect the independent restore receipt. Blocker: restore proof is pending.",
  "expected_revision": 0,
  "status": "paused"
}
```

Store new goals paused by default. Omit `status` on later objective edits to
preserve the current lifecycle. A `completed` goal requires explicit
`completion_evidence`; changing its objective cannot silently reuse the old
evidence. A completed turn never changes goal status. Budgets remain in the
existing policy/host checkpoint mechanisms; this API deliberately does not
pretend to meter tokens, dollars or iterations.

Saving/reading a goal does not launch, relaunch, resume, interrupt or call a
model. It never sends a separate continuation message. On the next ordinary
Omnigent user-message dispatch, the server reads the durable record and appends
its authoritative snapshot after the user's content. Claude receives it through
the existing terminal injection; OpenCode receives it through the existing
native prompt path. Model routing continues to use the original user request.
Existing terminal serialization, queueing, transcript ownership and model
configuration are preserved. Non-active states explicitly retain their hold.

This is goal context delivery, not a new autonomous scheduler or a hard
interruption mechanism. It does not intercept messages typed directly into a
vendor TUI, and it does not synchronize the separate host `goal.sh` watchdog.
Before importing existing checkpoints, preserve their original objectives,
steering, actual stop states, budgets and session bindings; reconcile competing
state sources rather than enabling an additional continuation loop.

Verify after adoption: save a paused goal while its runner is offline; read it
back with a new API client; confirm no runner or transcript item was created.
Edit using its revision and try the old revision again (409). On an already
approved normal message, confirm the exact objective/revision is present at the
native transport boundary and the paused hold remains. No paid inference is
needed for the transport acceptance test.

Tests:

```sh
.venv/bin/python -m pytest tests/stores/test_session_goals.py tests/server/routes/test_session_goals.py
```

This source patch must be integrated with the existing Serving owner's routing
and runner-preserving lifecycle candidate. Do not overwrite its worktree or
restart a busy host to adopt it. Deployment and the remaining live goal writes
are separate acceptance steps; the isolated tests do not claim production
adoption.
