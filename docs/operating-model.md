# Queue, supervisor, and progress model

## Sources of truth

SQLite is the operational source of truth. Agents use the deterministic JSON CLI. External task systems can supply `work_ref` values, but they do not authorize or control dispatch.

The package hash is the authority boundary:

```text
package -> proposed -> approved -> held or eligible -> running -> terminal
             |           |                                |
             v           v                                v
         cancelled    cancelled                      needs_review
```

`queue propose` validates a versioned package and workspace boundary. `queue approve` consumes an expiring single-use approval bound to that exact package hash and the `work.dispatch` capability. It creates the approved queue item. Dispatch reads only this stored package.

Priority is deterministic: higher number first, then earlier creation, then job ID. Dependencies must be `simulated` or `succeeded`. `not_before` must have passed.

## State machine

- `proposed`: validated package without dispatch authority.
- `approved`: authorized and available for eligibility checks.
- `held_dependency`: a dependency is not terminal-success.
- `held_schedule`: `not_before` is in the future.
- `held_policy`: usage or paid-credit safety denied dispatch.
- `held_capability`: hook, app, MCP, workspace, or runner capability check denied dispatch before a turn.
- `running`: a durable work run exists.
- `succeeded`: App Server reported completed and the post-run safety read passed.
- `needs_review`: outcome, interruption, overrun, or recovery is uncertain.
- `cancelled`: removed before execution.
- `simulating` and `simulated`: legacy fake-runner states only.

Runs use `starting`, `running`, `succeeded`, `failed`, `interrupted`, `blocked`, and `needs_review`.

## Dispatch sequence

```text
fresh quota probe
  -> package and approval hash check
  -> reserve, account, credit, integrity, concurrency gates
  -> durable run claim and job=running
  -> global hook/app/MCP inventory and exact deny overrides
  -> thread/start in read-only mode with thread-scoped capability config
  -> persist task ID
  -> verify effective feature, app, and MCP state
  -> fresh quota and lease renewal
  -> turn/start with approved sandbox and network off
  -> persist turn ID
  -> quota polling and lease renewal
  -> completed OR turn/interrupt
  -> fresh post-run quota check
  -> persist terminal state and audit event
```

The App Server method allowlist is fixed in code. Configuration cannot add methods or replace the `codex app-server --stdio` command. The runner can disable only non-managed hooks; an enabled managed hook blocks work. The built-in `codex_apps` runtime is disabled through the apps feature gate because it has no configurable MCP transport, then verified disabled after thread creation. Any server-initiated approval or tool request causes interruption.

The scheduler starts each task read-only as a project safety policy. The approved turn can then receive `workspaceWrite` with only the package cwd in `writableRoots` and network access false.

## Quota gates

Both the 5-hour and weekly windows must be present, fresh, internally consistent, and bound to the configured ChatGPT account. Admission subtracts expected usage multiplied by the policy margin. The projected remainder must preserve each reserve.

The full guarded estimate is reserved again at every in-run safety check. This is conservative: as observed usage rises, work can stop before the actual reserve floor.

Live dispatch always blocks explicit evidence that paid credits are available or spend control is reached. In the default `machine_only` mode, missing or null credit metadata is unknown and blocks work. The configured `operator_attested_subscription_only` mode accepts unknown credit metadata only after the operator explicitly asserts that the bound subscription account does not use paid credits. The existing fingerprint and plan binding makes an account change fail closed. A reported balance never increases scheduler headroom.

After a successful turn, the controller refreshes quota. It pauses and marks review if the reserve was crossed, credits became unsafe, the signal was lost, or the account-level usage delta exceeded the guarded estimate. The delta is an estimate because other Codex activity can share the account windows.

## Operator-driven loop

1. Read `status`, `queue list`, and `reconcile --dry-run`.
2. Propose packages with bounded objectives and conservative usage estimates.
3. Prepare and obtain an exact `queue.approve` approval.
4. Refresh `dispatch preflight` for the selected job.
5. Call `dispatch run` once with a unique idempotency key.
6. Use `monitor get` or `monitor refresh` for the returned run ID.
7. If a run becomes uncertain, stop new work, reconcile after lease expiry, inspect the Codex task, and create a new package only when retry is explicitly approved.

## Optional foreground supervisor

The disabled-by-default supervisor can repeat the same quota, eligibility, dispatch, monitoring, and recovery sequence. It acquires a durable singleton lease before polling and can select at most one already-approved immutable package per cycle. It cannot create approval, change a package, bypass controller state, or retry an uncertain run.

Probe failures use bounded exponential backoff with jitter. Expired work claims move to `needs_review`, and the controller pauses. Local notification events are stored in SQLite and an owner-only JSONL sink; they exclude prompts, objectives, task output, account identity, exact quota values, credentials, and App Server stderr.

The repository can render and validate a disabled LaunchAgent plist to a staging path. It has no install, load, enable, start, or uninstall command. Foreground use and launchd activation require separate local approval. See [Background service](background-service.md).

## Pause, stop, and recovery

- `pause` blocks the next dispatch. An existing turn continues while quota checks remain safe.
- `stop` changes the controller to `STOPPED`. The active runner detects that state at its next poll and requests `turn/interrupt`.
- A process crash leaves a run with a lease expiry. The task ID is persisted before turn start when possible.
- `reconcile --dry-run` lists stale candidates without mutation.
- Approved reconciliation sets the run and linked job to `needs_review` and leaves the controller paused.
- `monitor refresh` uses supported `thread/read` data to resolve known task state. It never reads or stores task content.
- Unknown outcome never triggers automatic retry.

## Redaction and account boundaries

The quota probe reduces the ChatGPT email to a keyed local fingerprint. The scheduler rejects account, plan, or limit-bucket changes. Audit records exclude objectives, prompts, output, repository content, raw identity, exact quota percentages, credentials, auth data, and stderr.

## Deferred operations

A launchd installer, live launchd activation, network API, external notifications, human UI, new authentication, and paid-credit fallback are outside the public-alpha operating model.
