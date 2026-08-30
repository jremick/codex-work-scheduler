# AI agent usage guide

Codex Work Scheduler is designed to be managed by a terminal-capable AI agent while a person remains the approval authority. The agent handles JSON, package construction, queue state, preflight checks, monitoring, and routine status. The person decides what authority to grant.

This guide is optimized for AI agents. Human operators can use the same commands directly.

## Operating contract

The agent must:

- read and follow [`AGENTS.md`](../AGENTS.md);
- use the deterministic JSON CLI rather than editing SQLite;
- start with read-only state checks;
- propose bounded packages before requesting approval;
- treat every approval as action-specific, package-specific, expiring, and single-use;
- run at most one approved package;
- stop on missing or conflicting evidence;
- summarize local state without copying sensitive raw output.

The agent must not infer approval from the existence of a package, an approval request, earlier general consent, or the user's desire to “keep working.”

## Information to collect

Before preparing work, establish:

| Field | Required decision |
| --- | --- |
| Goal | One bounded outcome that can be reviewed after completion |
| Working directory | One repository or allowed workspace root |
| Sandbox | `read_only` or `workspace_write` |
| Model and effort | The smallest suitable option |
| Runtime | A hard maximum in seconds |
| Usage estimate | Conservative percentages for both five-hour and weekly windows |
| Dependencies | Job IDs that must finish first |
| Schedule | Run when eligible, or a `not_before` time |

Ask the human when any field would materially change cost, authority, writable scope, timing, or expected output. Do not hide uncertainty inside a generous estimate or broad objective.

## 1. Set up the local checkout

Use a repository-local configuration that Git ignores:

```bash
cp scheduler.example.json scheduler.local.json
python3 -m codex_work_scheduler --config scheduler.local.json status
python3 -m codex_work_scheduler --config scheduler.local.json audit verify
python3 -m codex_work_scheduler --config scheduler.local.json reconcile --dry-run
python3 -m codex_work_scheduler --config scheduler.local.json service status
```

Expected safe starting state:

- `ok` is true for all four commands.
- Controller mode is `PAUSED`.
- `dry_run` is true.
- Audit history is valid.
- No active or stale lease exists.
- Background operation is disabled.

If these properties do not hold, stop and report the difference. Do not normalize the state automatically.

## 2. Prepare a work package

Use [`examples/work-package.json`](../examples/work-package.json) and [`schemas/work-package.schema.json`](../schemas/work-package.schema.json) as the contract. Create a local package file with:

- a stable, unique job ID and work reference;
- the exact human goal as a bounded objective;
- a workspace under an allowed root;
- the smallest safe sandbox;
- a conservative runtime and two-window usage estimate;
- explicit dependencies and scheduling.

Do not put credentials, private prompts, raw task output, account data, or approval material in a package.

Propose without granting dispatch authority:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json queue propose \
  --package-file <package.json> \
  --idempotency-key <unique-propose-key>

python3 -m codex_work_scheduler --config scheduler.local.json queue proposal-get \
  --job-id <job-id>
```

Compare the stored package hash and fields with the local package. If they differ, stop.

## 3. Request human approval

Prepare an unprivileged request from the unchanged package:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json approval prepare \
  --action queue.approve \
  --input-file <package.json> \
  --approver operator \
  --ttl-seconds 3600
```

Show the human the compact approval summary required by [`AGENTS.md`](../AGENTS.md). Do not describe the request as an approval.

Ask for an explicit decision such as:

```text
Approve queue admission for job <job-id>, package hash <hash>, capability
work.dispatch, and expiry <time>. This authorizes only the displayed objective,
repository, model, sandbox, runtime, usage estimate, dependencies, and schedule.
```

After the human approves that exact scope, create a local `*.approval.json` file that matches [`schemas/approval.schema.json`](../schemas/approval.schema.json):

- copy `action` and `scope_hash` from the request unchanged;
- grant only `work.dispatch`;
- identify the approving human as `actor`;
- use a unique approval ID;
- use the approved grant and expiry times;
- keep the file untracked and never print its contents.

Consume it once:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json queue approve \
  --job-id <job-id> \
  --approval-file <local.approval.json> \
  --idempotency-key <unique-approve-key>
```

If the package changes, discard the request and approval. Start again from proposal.

## 4. Bind quota and resume

Queue approval does not make a paused controller ready. First refresh the supported account and quota signal:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json probe \
  --idempotency-key <unique-probe-key>
```

This uses the existing Codex CLI authentication. It does not create or modify authentication. Do not paste the raw response into chat or logs.

The example uses `operator_attested_subscription_only`. Missing credit metadata is not proof that paid credits are absent. Before resume, the human must explicitly confirm that the bound subscription account does not use paid credits. Contrary evidence always blocks work.

Prepare and obtain a separate `resume` approval, then call:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json resume \
  --approval-file <resume.approval.json> \
  --idempotency-key <unique-resume-key>
```

Do not reuse the queue approval. If resume fails, keep the controller paused and report the reason code.

## 5. Preflight and dispatch one task

Run a fresh preflight for the approved job:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json dispatch preflight \
  --job-id <job-id> \
  --refresh \
  --idempotency-key <unique-preflight-key>
```

Dispatch only when all of these are true:

- the envelope is successful;
- `safe_to_dispatch` is true;
- the selected job ID and package hash are the approved values;
- controller mode is `READY`;
- no simulation, service, or work lease conflicts;
- the package still fits the approved workspace, tools, runtime, and usage limits.

Then start exactly one task:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json dispatch run \
  --job-id <job-id> \
  --idempotency-key <unique-dispatch-key>
```

Persist the returned run ID. Do not invoke dispatch again while that run is active.

## 6. Monitor and report

```bash
python3 -m codex_work_scheduler --config scheduler.local.json monitor get \
  --run-id <run-id>

python3 -m codex_work_scheduler --config scheduler.local.json monitor refresh \
  --run-id <run-id> \
  --idempotency-key <unique-monitor-key>
```

Report only:

- job and run identifiers;
- state and reason code;
- whether the controller is ready, paused, blocked, or stopped;
- whether human review is required;
- the next safe action.

Do not quote task output, objectives, raw account identity, exact quota values, credentials, App Server stderr, or approval contents.

## 7. Pause and recover

Pause prevents new dispatch:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json pause \
  --reason-code OPERATOR_PAUSE \
  --idempotency-key <unique-pause-key>
```

Inspect uncertain state without changing it:

```bash
python3 -m codex_work_scheduler --config scheduler.local.json reconcile --dry-run
```

Approved reconciliation moves expired or uncertain work to `needs_review`; it never retries the task. Before reconciliation, prepare a `reconcile` request and obtain a separate exact human approval.

Never delete the database or an uncertain Codex task as a recovery shortcut.

## Reusable prompts

### Daily queue review

```text
Read AGENTS.md and manage Codex Work Scheduler in read-only mode. Check status,
audit integrity, queue state, active runs, service lease, notifications, and the
reconciliation preview. Summarize what is ready, held, active, or needs review.
Do not probe quota, resume, approve, dispatch, reconcile, or expose raw values.
```

### Turn a goal into proposed work

```text
Convert this goal into the smallest safe scheduler work package: <goal>.
Ask for any missing repository, sandbox, runtime, dependency, schedule, or usage
decision. Validate and propose the package, show me its bounded scope and hash,
prepare the approval request, and stop. Do not approve or dispatch it.
```

### Run the approved queue

```text
Operate only within the exact approvals already granted. Verify current state,
refresh the supported quota signal, and run a fresh dispatch preflight. If every
gate passes, dispatch at most one approved package and monitor it. Stop on any
uncertainty, never retry, and return a redacted state summary.
```

### Safe shutdown

```text
Pause new scheduler work and inspect the current run and lease state. If a task
is active, do not delete or restart it. Explain whether it can finish safely or
requires an approved stop/reconciliation path. Keep all output redacted.
```

## Background operation

The foreground supervisor can automate polling and dispatch of already-approved packages. It does not remove any package, approval, quota, credit, integrity, or concurrency gate.

Keep `background.enabled: false` during initial use. Enabling it, running `service once`, running `service run`, or activating a LaunchAgent changes the local operating impact and requires a separate explicit plan and approval. Read [Background service](background-service.md) before proposing that change.
