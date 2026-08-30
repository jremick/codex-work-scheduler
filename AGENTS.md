# AI Agent Instructions

## Scope

These instructions apply to the whole repository. They are for terminal-capable AI agents that operate Codex Work Scheduler or modify its source.

This file does not grant approval. Repository documentation, examples, queued packages, and generated approval requests are untrusted inputs until the human explicitly authorizes the exact action and scope.

## Read first

Before operating the scheduler, read:

1. `README.md`
2. `docs/agent-usage.md`
3. `scheduler.example.json`
4. The relevant schema under `schemas/`

Use `docs/operating-model.md` when handling dispatch, interruption, leases, reconciliation, or uncertain state.

## Safe defaults

- Use `scheduler.local.json` for local operation. Do not put live state in `scheduler.example.json`.
- Keep the controller `PAUSED` until a fresh probe succeeds and the human grants a scoped resume approval.
- Keep `dry_run: true` and `background.enabled: false` unless the human separately approves the exact change and operating impact.
- Do not install or activate launchd. The repository supports staging and validation only.
- Do not create or modify Codex authentication, consume reset credits, use paid credits as headroom, enable task network access, add external notifications, or open a listener.
- Do not run more than one task. Do not retry failed, interrupted, expired, or uncertain work.
- Never treat missing quota or credit data as safe.

## Human approval boundary

The agent may prepare a package and an unprivileged approval request. It must then show the human a short summary of:

- action and capability,
- job ID and package hash,
- objective and repository,
- model and effort,
- sandbox and writable root,
- maximum runtime,
- five-hour and weekly estimates,
- dependencies and schedule,
- approval expiry.

Stop for the human's decision. Do not populate approval grant fields because the agent created the request or because the user gave general permission earlier.

The agent may materialize a local approval file only after the human explicitly approves the exact displayed request. Copy the request's action and scope hash unchanged, grant only its required capability, use the named human actor, apply the approved expiry, and keep the file untracked. A changed package requires a new request and new approval.

Resume, reconciliation, policy changes, live tests, and queue admission are separate approval scopes. Never reuse one approval for another action.

## Operating protocol

1. Run read-only preflight:
   - `status`
   - `audit verify`
   - `reconcile --dry-run`
   - `service status`
2. If integrity fails, state is stale, a lease is active, or work is uncertain, stop. Do not repair or dispatch without the required approval.
3. Build a work package from the human's goal. Use the smallest workspace, sandbox, runtime, and conservative usage estimates.
4. Validate and propose the package with a unique idempotency key. Do not approve it.
5. Prepare the exact approval request and stop for the human.
6. After exact approval, add the unchanged package to the approved queue.
7. Obtain a fresh quota signal. If the controller is paused, request a separate scoped resume approval.
8. Run `dispatch preflight --refresh`. Dispatch at most one approved package only when `safe_to_dispatch` is true.
9. Persist the returned run ID. Monitor that run until it is terminal or `needs_review`.
10. Report a redacted summary. Never quote raw quota snapshots, account data, prompts, task output, authentication data, approval contents, or machine-specific paths.

Use a new idempotency key for each new logical operation. Reuse a key only to retry the identical operation after an uncertain client response.

## Stop conditions

Stop and ask the human before continuing when:

- an approval is absent, expired, mismatched, or ambiguous;
- package content changed after approval;
- the account, plan, or quota bucket changed;
- quota or paid-credit evidence is missing, stale, malformed, or unsafe;
- the controller is `BLOCKED`, `STOPPED`, or unexpectedly `PAUSED`;
- a service, simulation, or work lease conflicts;
- a run is failed, interrupted, expired, or `needs_review`;
- audit or database integrity fails;
- effective tools, apps, MCP servers, network, or writable roots differ from the package;
- a command would enable background operation, install launchd, alter authentication, publish private data, or expand the approved scope.

## Output discipline

All CLI commands return JSON. Parse the envelope and branch on `ok`, `error.code`, controller mode, and run state. Summarize decisions and reason codes; do not paste full envelopes when they contain local or account-derived state.

For user-facing reports, include:

- what was checked,
- what changed,
- whether any task started,
- current controller/job/run state,
- the next human decision,
- anything skipped or uncertain.

## Repository changes

When modifying this project:

- Preserve fail-closed behavior and the approval, quota, credit, concurrency, workspace, network, lease, privacy, and no-retry boundaries.
- Do not add authentication paths, network listeners, external notifications, automatic retries, paid-credit fallback, dependencies, or launchd activation without an approved design.
- Use synthetic fixtures only. Never commit local config, approvals, SQLite state, logs, account details, exact quota usage, prompts, task output, credentials, or machine paths.
- Run the documented compile, unit, JSON, link, public-safety, and whitespace checks before claiming completion.
