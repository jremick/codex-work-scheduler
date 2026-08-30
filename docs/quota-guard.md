# Quota guard

Quota guard is a disabled-by-default control for existing Codex tasks. It inventories active tasks, binds an exact selected set to one approval, checks account quota on a chosen interval, and contains those tasks when either the five-hour or weekly remaining quota reaches the configured threshold.

This feature controls tasks. It does not delete tasks or worktrees, change authentication, consume reset credits, use paid credits, install a service, or enable launchd.

## Safety boundary

The example configuration keeps both `quota_guard.enabled` and `background.enabled` set to `false`. Arming a guard requires both settings to be enabled in the reviewed local configuration and requires the background poll interval to be no longer than the selected guard interval.

Activation is a separate operational decision because a guard can pause goals, interrupt active turns, and later start a fixed continuation turn. Source installation and tests do not activate it.

The task-control App Server method allowlist is fixed in code:

- `thread/list` and `thread/read`
- `thread/goal/get` and `thread/goal/set`
- `turn/interrupt`
- `thread/resume` and `turn/start`

The adapter never sends an objective when it changes goal status. It never stores a task title, prompt, objective, output, or goal usage record.

## Plan and approval

First inventory active tasks. This read is transient and does not create a guard:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json quota-guard inventory
```

Refresh the supported account-bound quota signal before preparing approval:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json probe \
  --idempotency-key quota-guard-probe-001
```

Create a plan that names only the selected task IDs:

```json
{
  "schema_version": "1",
  "threshold_remaining_percent": 10,
  "check_interval_seconds": 900,
  "target_thread_ids": ["thread-id-one", "thread-id-two"],
  "resume_non_goal_threads": true
}
```

The threshold uses remaining quota. A value of `10` means containment begins when either supported window has 10% or less remaining. Detection can occur up to one configured interval after the account crosses the threshold.

Prepare the exact approval scope:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json approval prepare \
  --action quota-guard.arm --input-file quota-guard-plan.json
```

The scope also binds the configured account profile, limit bucket, and resume hysteresis. Materialize an approval only after reviewing that hash and the selected task IDs. Then arm it with a unique idempotency key:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json quota-guard arm \
  --plan-file quota-guard-plan.json --approval-file quota-guard-approval.json \
  --idempotency-key quota-guard-arm-001
```

Only one guard can be active. The selected target set is durable in local SQLite. The approval does not authorize any unlisted task.

The approval expiry limits when the guard can be armed. A successful arm creates durable recurring authority for the exact bound plan until the operator disarms it or the guard enters `NEEDS_REVIEW`; later interval checks do not extend or broaden that scope.

## Stop and resume sequence

Each due cycle runs targets sequentially.

When quota is at or below the threshold, or its signal becomes missing, stale, malformed, or account-mismatched, the guard contains selected tasks:

1. Read the task and its exact active turn ID.
2. If an active `/goal` exists, set only its status to `paused` and verify it.
3. Read the task again to avoid using a turn ID captured before the goal pause.
4. Commit a `STOPPING` claim before the external interrupt.
5. Interrupt only the pinned turn ID and verify that exact turn is terminal.
6. Verify that no replacement active turn appeared after the interrupt.
7. Hold the target. Any uncertain response becomes `NEEDS_REVIEW` and is not retried.

While held, each due cycle checks for a manual restart and contains it again. Missing or uncertain quota can never cause resume.

Resume requires all of these conditions:

- both remaining-quota windows are above `threshold + resume_hysteresis_percent`;
- every window that caused the stop has a later reset timestamp;
- usage in each tripped window fell after that reset;
- every selected target remains in a verified held state;
- paid-credit metadata reports `unavailable` and spend control reports `not_reached`;
- the scheduler controller is not `BLOCKED` or `STOPPED`;
- no target is in `NEEDS_REVIEW`.

If the guard paused an active `/goal`, it records the goal update timestamp as an ownership token. It restores the goal only when the paused status and timestamp still match that token, then verifies a later active timestamp. A human or another client changing the goal while held therefore blocks automatic resume. It does not activate a goal that was already paused before containment. For a non-goal task, continuation occurs only when `resume_non_goal_threads` was approved. The adapter reopens the same task with approval policy `never` and a read-only sandbox, then sends one fixed continuation instruction with networking disabled. The instruction refers only to the existing context and approved scope. It never replays the previous user prompt.

Quota guard does not automatically set the scheduler controller back to `READY`. Guarded task resume and scheduler queue dispatch are separate authorities.

## Status, canary, and disarm

Read durable state without probing quota or changing a task:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json quota-guard list
python3 -m codex_work_scheduler --config scheduler.local.json quota-guard status --guard-id <guard-id>
```

After separate approval for a bounded live canary, the existing singleton supervisor can refresh quota and process one cycle. Keep the scheduler controller `PAUSED` and confirm that no queue package is eligible for dispatch:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json service once
```

Normal recurring operation uses the existing foreground supervisor. Enabling it or installing a LaunchAgent remains outside this implementation step and requires the activation and rollback process in [Background service](background-service.md).

Disarm stops future guard actions. It deliberately does not resume held tasks:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json quota-guard disarm \
  --guard-id <guard-id> --idempotency-key quota-guard-disarm-001
```

Disarm cannot cancel an App Server request that was already in flight. The guard checks the durable disarmed state before each later mutation and never begins a resume after observing it.

## Current limitation: task writer ownership

Task control depends on access to the running Codex App Server's local control socket. The guard treats an unavailable socket, ownership rejection, timeout, mismatched response, stale turn ID, or unverified terminal state as `NEEDS_REVIEW`. It does not retry or start a replacement task.

The adapter uses the fixed `codex app-server proxy` command to reach the already-running App Server control socket instead of starting a competing writer. If that socket is absent, inaccessible, or owned by an incompatible App Server, the guard fails closed. The fake App Server suite verifies the protocol and state machine. Live control of desktop-owned tasks remains an explicit canary requirement before operational activation.
