# Getting started

Codex Work Scheduler is easiest to use through a local AI coding agent. The agent handles the JSON CLI and queue mechanics; you approve the exact work and operating changes.

## What you need

- macOS
- Python 3.9 or later
- Git
- Codex CLI using its normal existing sign-in
- A terminal-capable AI agent such as Codex

The project uses only Python's standard library. It does not install a package, create a web service, or add another authentication flow.

## Agent-managed setup

Give your agent this instruction:

```text
Clone https://github.com/jremick/codex-work-scheduler.git and set it up for a
safe local evaluation. Read README.md, AGENTS.md, and docs/agent-usage.md first.
Copy scheduler.example.json to scheduler.local.json, run the repository checks,
then verify status, audit history, reconciliation preview, and service status.

Do not probe my account, create approval files, resume, dispatch, enable the
background service, or activate launchd. Report only redacted state and stop on
any failed or unexpected check.
```

The agent should report:

- Tests and repository checks passed.
- Controller mode is `PAUSED`.
- `dry_run` is true.
- Audit history is valid.
- No work, stale state, or service lease is active.
- Background operation is disabled.

If the agent reports anything else, keep the controller paused and review the difference.

## Your first work item

Ask the agent to prepare work without running it:

```text
Prepare the smallest safe scheduler package for this goal: <goal>.
Use <repository>, explain the model, sandbox, time limit, usage estimate,
dependencies, and schedule. Validate and propose it, prepare the scoped approval
request, and stop for my decision. Do not approve, resume, or dispatch.
```

Review the summary. If it is correct, explicitly approve the displayed job, package hash, capability, and expiry. The agent can then create the local approval file for that exact scope.

Queue approval and controller resume are separate decisions. Before live work, the agent must obtain a fresh supported quota signal and a separate resume approval. The full machine-oriented sequence is in the [AI agent usage guide](agent-usage.md).

## Manual setup

To perform the safe setup yourself:

```bash
git clone https://github.com/jremick/codex-work-scheduler.git
cd codex-work-scheduler
cp scheduler.example.json scheduler.local.json

python3 -m py_compile codex_work_scheduler/*.py
python3 -m unittest discover -s tests -v
python3 scripts/check_json.py
python3 scripts/check_local_links.py
python3 scripts/check_public_safety.py --history

python3 -m codex_work_scheduler --config scheduler.local.json status
python3 -m codex_work_scheduler --config scheduler.local.json audit verify
python3 -m codex_work_scheduler --config scheduler.local.json reconcile --dry-run
python3 -m codex_work_scheduler --config scheduler.local.json service status
```

`scheduler.local.json`, `.scheduler/`, SQLite files, and approval files are ignored by Git. The commands above use synthetic tests and local state. They do not read account usage or start Codex work.

## Routine manual checks

```bash
python3 -m codex_work_scheduler --config scheduler.local.json status
python3 -m codex_work_scheduler --config scheduler.local.json queue list
python3 -m codex_work_scheduler --config scheduler.local.json monitor list
python3 -m codex_work_scheduler --config scheduler.local.json notifications list --limit 20
python3 -m codex_work_scheduler --config scheduler.local.json reconcile --dry-run
python3 -m codex_work_scheduler --config scheduler.local.json audit verify
```

All commands return JSON. Do not publish raw output from live operation because it can contain local state or normalized account-derived data.

## Background operation

The optional foreground supervisor is disabled by default. Start with explicit agent-managed or manual commands.

The repository can render a disabled LaunchAgent plist to a staging directory, but it does not install or load launchd. Enabling the supervisor or activating launchd requires a separate local plan and approval. See [Background service](background-service.md).
