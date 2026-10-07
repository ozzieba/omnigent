# Codex sessions: llmq admission gate and per-session Turbo

Owner: Fable orch (delivery). Runtime rollout: Claude orch. llmq source owner: codex-root-orchestrator/model-currency-apps.
Status: design for review, 2026-10-07. Decision record for oz's 2026-10-06 instruction.

## Decisions this design implements

oz, 2026-10-06, verbatim: "gate via llmq, but with a per session 'turbo' button on omnigent right next to the model selector".

oz, 2026-10-07, verbatim: "this is not a security boundary; in fact llmq should fail open but with an indicator".

So:

1. Every fleet Codex turn asks llmq's native-turn admission before it starts. llmq keeps the 90% credit-safety margin
   (`CREDITED_INCLUDED_MAX_USED_PERCENT`, PR #1208) for fresh evidence.
2. Turbo is a per-session boolean on the Omnigent session, off by default, toggled from the web UI next to the model
   selector. With Turbo on, that session's turns may pass the margin and spend paid credits; llmq admits them as
   credit-bearing reservations so settlement records them.
3. Turbo is a convenience toggle, not a security control. The runner passes the flag to llmq as a plain field. Any
   caller with edit access to the session may toggle it. Every toggle is logged and shown in the session.
4. Fail open with an indicator. If llmq is unreachable, times out, answers with an unknown shape, or cannot decide
   (stale or missing receipt, unreviewed response shape, missing permission field), the turn runs and the session
   shows a "quota unverified" indicator plus a note on the turn. The only refusal is fresh evidence showing the
   session at or above the margin with Turbo off, and then the session pauses with a clear message and resumes
   cleanly.
5. llmq's live Codex admission (#1208) changes from fail-closed to fail-open-with-indicator for the "cannot decide"
   cases, returning an allowed-but-unverified decision with a displayable reason.

## Where the gate hooks in

### What exists today

- llmq already has the native-turn API (`/v1/native-turns/{admissions,activations,cancellations,settlements}`,
  `llm-quota-controller/src/llm_quota_controller/native_turns.py`). A Codex admission carries a `codex_receipt`
  (account and quota responses from the Codex app-server) that `codex_native_contract.subscription_evidence`
  validates and the engine books against `quota_windows`.
- The receipt comes from the `llmq-codex-native-receipt` timer on r820 (`native_receipt.py`), which polls the app-server
  every minute, posts the observation batch to `/v1/observations`, and writes
  `<state_dir>/codex-native-receipt.json` on r820 only.
- Claude turns are gated by a harness hook: `~/.claude/settings.json` on r820 runs `llmq claude-hook --event
  UserPromptSubmit|Stop` when `OMNIGENT_QUOTA_ROUTE=personal-llmq` and `LLMQ_AGENT_ID` are set. The hook
  (`native_hook.py`) reads a local evidence file, admits, waits for throttling, and writes a quota-wait file that the
  Omnigent runner's `omnigent/runner/quota_wait.py` watcher turns into session status. There is no `codex-hook`
  subcommand, p16v has no llmq hook configured at all, and p16v and tpe have no local Codex receipt.
- Omnigent sets `OMNIGENT_QUOTA_ROUTE=personal-llmq` on every host launch frame (`omnigent/host/connect.py`) and
  passes `LLMQ_AGENT_ID`, `OMNIGENT_QUOTA_ROUTE` through to Codex launches (`omnigent/inner/codex_executor.py`,
  `_CODEX_OMNIGENT_LAUNCH_ENV_VARS`). Live r820 Codex app-servers already carry `OMNIGENT_QUOTA_ROUTE=personal-llmq`.

### Codex entry points in the fleet

| entry point | how a turn starts | gate |
|---|---|---|
| Omnigent `codex-native` sessions (p16v, r820, tpe), including every agentq lane (agentq creates them with `POST /v1/sessions`) | the runner's executor issues `turn/start` to the Codex app-server (`omnigent/inner/codex_native_executor.py`, and `omnigent/inner/codex_executor.py` for the wrapped `codex` harness) | A: runner pre-turn gate |
| Omnigent terminal-UI Codex sessions (`omnigent.wrapper=codex-native-ui`, which agentq sets for permission level 4 lanes; Codex root orch, storage) | the user or agent types into the `codex` TUI; the runner does not see the turn | B: Codex `UserPromptSubmit` hook in the generated `CODEX_HOME/hooks.json` |
| `fleetctl native-launch` (`infra/omnigent-agent-fleet/fleetctl.py:run_native_launch`) | runs one fixed native harness under the host cap via `subprocess.run`; for Codex this is a TUI launch | B, through the same generated hooks |
| llmq's own Codex gateway lane (`codex_gateway.py`) | already admits through the engine | unchanged |

A direct `codex` run outside Omnigent and outside fleetctl is not gated. That is accepted: fleet agents launch through
Omnigent or fleetctl, and this is not a security boundary.

### Decision: one admission client, two entry points

Both entry points call one module, `omnigent/runner/llmq_gate.py`:

```
admit_codex_turn(session_id, model, turbo, *, agent_id, controller_url, token) -> GateDecision
settle_codex_turn(decision, usage | None)
GateDecision = allow: bool, verified: bool, reason: str, resets_at: float | None, reservation_id: str | None,
               credit_bearing: bool
```

- It sends `POST /v1/native-turns/admissions` with `harness=codex`, `evidence="controller"` (no local receipt),
  `turbo=<session flag>`, a 5 s timeout, no retries.
- Fail open: connection error, timeout, HTTP 5xx, non-JSON or unknown-shape body, or 4xx other than a decision all
  produce `allow=True, verified=False, reason=<slug>`.
- Refusal only when the body is a well-formed decision with `allowed=false` and `reason="credit_safety_margin"`.
- Throttling (`allowed=true` with `admission_not_before` in the future) keeps today's semantics: wait up to the
  existing `LLMQ_MAX_ADMISSION_DELAY_SECONDS` cap and publish the wait like `quota_wait.py` does for Claude.

A (runner pre-turn gate): in the executor, immediately before `turn/start`, with the session's current Turbo flag
read fresh from `GET /v1/sessions/{id}` (the same fetch `_fetch_session_model_override` does for `/model`). `turn/steer`
is not a new turn and is not gated. On refusal the executor does not issue `turn/start`; it posts
`external_session_status(status="blocked", blocked_on="quota", quota_wait={...})` with the message below, re-checks
every 30 s (and immediately on a Turbo toggle, which the server pushes as a session update), then issues the turn
unchanged when admitted. The user message is never dropped. On a verified admission the executor activates the
reservation and settles it at turn end with the app-server's token-usage notification; if the app-server gives no
usage for the turn, it settles with zero usage so llmq retains its estimate rather than leaking a lease.

B (TUI hook): the generated `CODEX_HOME/hooks.json` (which orchestration already writes for routing, bypassing
Codex's hook-trust prompt) gets a `UserPromptSubmit` entry running `python -m omnigent.codex_llmq_hook`. The hook
reads its session id and server coordinates from the bridge dir like the existing router hook, calls the same
`admit_codex_turn`, and on refusal returns `{"decision": "block", "reason": <message>}` so the TUI shows the message
and keeps the prompt in the composer; on admission it returns `{}`. It also posts the same status and indicator
through the server. Settlement from the TUI path is best effort (the `Stop` hook carries no Codex usage today):
the hook settles with zero usage at the next prompt or after `lease` expiry; llmq's existing lease expiry covers
the rest.

### Quota route scope

Only sessions whose launch carries `OMNIGENT_QUOTA_ROUTE=personal-llmq` are gated. Work-route sessions are untouched.

## Data model

### Omnigent

- `Conversation.turbo: bool = False`, stored as `"turbo": "on"` in the packed `conversations.session_overrides`
  JSON blob with the other per-session switches (`model_override`, `cost_control_mode_override`, ...), so no
  migration is needed and the flag survives every other override write. (Implemented this way in the first PR
  instead of the column first proposed here.)
- `SessionResponse.turbo`; `UpdateSessionRequest.turbo: bool | None`; create accepts `turbo`.
- `PATCH /v1/sessions/{id} {"turbo": true|false}` at `LEVEL_EDIT`, the same level as `model_override`. The handler
  writes one log line (`turbo session=<id> user=<id> <old>-><new>`) and publishes a `session.turbo` SSE event so
  every open surface re-renders the control and chip. The persistent "Turbo" chip in the composer's config summary
  is the visible state; the transcript note first proposed here is left to the runner (phase 3), which already
  posts quota status into the session.
- Session status carries a `quota` object on the session's external status:
  `{"state": "verified" | "unverified" | "blocked", "reason": str, "resets_at": float | None, "turbo": bool}`.
  The web client renders it next to the model selector.

### llmq

- `NativeTurnAdmission` gains `turbo: bool = False` and `evidence: Literal["caller", "controller"] = "caller"`.
  With `evidence="controller"` the Codex `codex_receipt` is omitted and the controller loads its own latest receipt
  from `Settings.codex_receipt_file` (default `<state_dir>/codex-native-receipt.json`, the file the publisher
  already writes). Caller-supplied receipts keep working.
- `AdmissionDecision` gains `verified: bool = True`, `unverified_reason: str | None`, `resets_at: float | None`,
  `credit_bearing: bool = False`. Existing callers ignore the new fields.
- `SubscriptionEvidence` gains `credit_bearing: bool`. Reservations made from credit-bearing evidence carry
  `metadata.native_credit_bearing = true` and `native_turbo = true`; settlement is unchanged and records against
  that reservation, so credit use is attributable per turn and per agent.

## llmq admission semantics (the #1208 change)

`subscription_evidence(..., turbo: bool)` and the `/v1/native-turns/admissions` route classify every outcome into
one of three:

| evidence state | Turbo off | Turbo on |
|---|---|---|
| fresh, credited account, every ordinary window `usedPercent < 90` | admitted, verified | admitted, verified |
| fresh, credited, any ordinary window `>= 90` and `< 100` | **refused**: `reason=credit_safety_margin`, `resets_at` set | admitted, verified, `credit_bearing=true` |
| fresh, credited, `usedPercent >= 100` (included quota exhausted) | **refused**: `credit_safety_margin` | admitted, `credit_bearing=true`, no window booking (credits are the capacity) |
| fresh, `ordinaryUsageAllowed` explicitly `false` | **refused** (treated as exhausted) | admitted, `credit_bearing=true` |
| credit-free account | unchanged: admitted while `< 100`, refused at exhaustion with `resets_at` | same (no credits to spend) |
| receipt missing, `unavailable`, or older than 300 s; generation mismatch; windows incomplete | admitted, `verified=false`, `unverified_reason` one of `receipt_missing`, `receipt_stale`, `generation_changed`, `windows_incomplete` | same |
| unreviewed shape (unknown bucket, credit keys, window keys), `ordinaryUsageAllowed` absent | admitted, `verified=false`, `unverified_reason=unreviewed_shape` or `permission_unknown` | same |
| engine `no_fresh_quota_telemetry` / `provider_permission_unknown` | admitted, `verified=false` | same |
| engine throttling (rate shaping below the margin) | unchanged: admitted with `admission_not_before` | unchanged |
| identity or scope mismatch, bad request | 403 / 422 as today (the client fails open on these) | same |

Unverified admissions create no reservation (`reservation_id=null`); nothing to activate or settle. They are
persisted as decisions with the reason so they are countable, and the controller logs one line per unverified
admission with the slug.

The 90% constant and its rationale from #1208 are unchanged. The margin check now needs `resets_at`: the contract
raises `CreditSafetyMarginReached(resets_at=<earliest ordinary primary reset>)`, a `CodexContractError` subclass,
and the route maps it to the refusal above instead of 503.

## Messages and indicators

- Refusal message (status line, transcript note, TUI block reason):
  "Codex subscription quota is at the credit-safety margin (90%); resets <local time, relative>. Enable Turbo on this
  session to use paid credits."
- Unverified indicator: a small "quota unverified" badge beside the Turbo toggle with the slug on hover
  ("llmq unreachable", "receipt stale", ...), and a one-line note on the turn in the transcript. Cleared on the next
  verified admission.
- Turbo on: the toggle shows filled/accent with the label "Turbo: paid credits allowed" and the session header shows
  a persistent "Turbo" chip. The toggle log line and transcript item are written on every change.

## Web UI

- `web/src/pages/ChatPage.tsx`: a `TurboToggle` immediately right of `RoutingModelSelect` in the composer
  (`componentId="chat.composer.turbo"`), reading `conv.turbo`, calling `PATCH /v1/sessions/{id}`, optimistic with
  rollback on error; the quota badge next to it driven by the session's external status `quota` object.
- `web/src/shell/NewChatDialog.tsx`: the same toggle in the model row of the run-config knobs for Codex harnesses,
  posted as `turbo` on create.
- Codex-only: hidden for harnesses that never route through `chatgpt-codex`.

## Threat model (informational, since this is not a security boundary)

- An agent can toggle Turbo on its own session via `PATCH` (edit level). Accepted by oz. The toggle log and the
  transcript item make it visible; llmq's `native_turbo` metadata makes the spend attributable.
- The runner asserts `turbo` to llmq; llmq does not verify it. Accepted.
- Fail-open means an llmq outage lets Codex sessions spend credits at 100% quota. Accepted; the indicator and the
  controller log make the window observable, and the publisher's `native-unavailable` window still blocks the
  llmq-served Codex lane as today.
- Tokens: each host needs an llmq token with role `agent` (or `native-agent` scoped to `codex`) whose actor equals
  that host's `LLMQ_AGENT_ID`. Observer tokens cannot admit; with one, the gate fails open with
  `unverified_reason=forbidden`, which the indicator shows, so a mis-provisioned host is visible rather than silent.

## Tests

llmq (`llm-quota-controller/tests/test_codex_turbo_admission.py`, extending `test_codex_credit_safety_margin.py`):
- contract matrix at 89, 90 and 100 `usedPercent`, Turbo off and on, credited account: outcomes as in the table,
  including `credit_bearing` and `resets_at`.
- route: stale receipt, missing receipt file, `unavailable` receipt, unreviewed shape, absent `ordinaryUsageAllowed`,
  explicit `false`, generation mismatch: each returns 200 with `allowed=true, verified=false` and the slug, no
  reservation; explicit `false` refuses with Turbo off and admits credit-bearing with Turbo on.
- engine: credit-bearing admission creates a reservation with `native_credit_bearing`, books no window, settles.
- `evidence="controller"` loads the publisher file; `evidence="caller"` keeps byte-identical behaviour for existing
  callers (the existing margin tests pass unchanged).

Omnigent:
- server: PATCH `turbo` at edit level, response carries it, log line and transcript item written, create with
  `turbo`, migration up/down on SQLite and Postgres heads single.
- runner `llmq_gate`: fail-open on connection error, timeout, 5xx, non-JSON, unknown shape, 403; refusal pauses and
  posts the status with `resets_at`; re-admission after a Turbo toggle resumes with the original message; verified
  admissions activate and settle; throttling waits and publishes.
- executor: `turn/start` is not issued while blocked; `turn/steer` is not gated.
- hook: block reason and `{}` on admission; fail-open on every fault; no secrets or paths in output.
- web: toggle renders off by default, PATCHes on click, shows the on state and chip, badge appears for
  `quota.state=unverified` and the blocked message for `blocked`; New Chat posts `turbo`.

## End-to-end canary plan (runtime, Claude orch)

1. llmq release (per-PR `host_release.py`, as #1208): read-only admission simulation against the live receipt with
   Turbo off and on; both verified while the window is below 90%.
2. One Omnigent `codex-native` session on r820, Turbo off, quota below the margin: a turn is admitted and verified,
   its reservation settles with usage; the session shows no badge.
3. Same session with the controller stopped for one turn: the turn runs, the badge reads "quota unverified: llmq
   unreachable", the controller log (after restart) has no entry, the runner log has the slug.
4. A staging controller (`llmq serve` on another port with a copied DB and a synthetic 95% receipt file), one session
   pointed at it via `LLMQ_CONTROLLER_URL`: the turn pauses with the margin message and `resets_at`; toggling Turbo
   in the UI resumes it within 30 s and the staging ledger shows a `native_credit_bearing` reservation.
5. TUI path: one `codex-native-ui` session against the staging controller: the hook blocks with the message, Turbo
   on lets the next prompt through.
6. Rollback: llmq `host_release.py rollback`; Omnigent wheels re-pin; the UI degrades to no toggle.

## Phases and PRs

1. llmq: turbo, controller evidence, fail-open classification, tests. chatgpt-playground PR against the controller
   stack branch (`agent/llmq-current-model-adoption-stack-20261006`), reviewed with model-currency-apps.
2. Omnigent server + web: column, PATCH, response, toggle, badge, transcript item. oz/omnigent PR.
3. Omnigent runner: `llmq_gate`, executor pre-turn gate, status, settlement, TUI hook. oz/omnigent PR (can stack on 2).
4. Rollout and canary: Claude orch, with per-host llmq tokens as the prerequisite.

## Open questions for review

- Token provisioning: one `agent`-role token per host, or one fleet token with `delegate_actor` and
  `x-llmq-actor` set to the session's `LLMQ_AGENT_ID`? The latter keeps per-session attribution with one secret.
- Re-check cadence while blocked: 30 s proposed; the receipt refreshes every 60 s, so 30 s is the useful floor.
- Should an unverified admission with Turbo off also be counted toward a daily "unverified turns" ceiling that
  turns the indicator into a refusal? Not in this design; oz chose fail-open.
