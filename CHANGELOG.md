# Changelog

All notable public changes will be recorded here. The project uses alpha releases and does not promise backward compatibility before beta.

## 0.7.0-alpha.1 - 2026-08-31

### Added

- A disabled-by-default quota guard that inventories an exact selected set of existing Codex tasks and binds the threshold, check interval, target IDs, and non-goal resume policy to one scoped approval.
- Durable quota-guard sessions and targets with singleton enforcement, optimistic revisions, crash fencing, redacted audit events, and an internal state migration from version 1 to 2.
- A fixed local task-control adapter that uses `codex app-server proxy` with an immutable method allowlist. It stores task and turn identifiers and states, but not titles, prompts, objectives, output, or goal usage.
- CLI commands for quota-guard inventory, arm, list, status, and disarm operations.

### Safety notes

- Either supported quota window can trigger containment. Missing, stale, malformed, account-mismatched, or failed quota signals force immediate containment even before the next scheduled guard check.
- `/goal` resume requires an unchanged guard-owned pause token. Resume also requires reset proof, hysteresis, safe paid-credit and spend-control signals, no active or replacement turn, and an eligible controller mode.
- Ambiguous task control, writer ownership, manual task changes, process crashes, dispatch races, and unverified outcomes become `NEEDS_REVIEW` without automatic retry.
- Non-goal continuation is opt-in, reopens the same task with approval policy `never`, and starts one fixed continuation in a read-only, network-disabled sandbox.
- Operational activation still requires a separately approved live control-socket canary. This release does not activate background operation, install launchd, delete tasks or worktrees, add authentication, consume reset credits, use paid credits as headroom, publish a package, or open a listener.

## 0.6.0-alpha.1 - 2026-08-30

### Added

- A local fail-closed controller for approved Codex work with durable SQLite state, dual-window usage reserves, immutable package approvals, bounded dispatch, monitoring, reconciliation, and redacted audit records.
- A deterministic JSON CLI for queue, approval, quota, dispatch, monitor, recovery, and audit workflows.
- A disabled-by-default foreground supervisor with singleton ownership, bounded polling, local redacted notifications, and no automatic retry of uncertain work.
- A staging-only disabled LaunchAgent plist renderer and validator. The repository does not install or activate launchd.
- An AI-first README, machine-readable `AGENTS.md`, reusable agent prompts, and a detailed agent operating guide with a manual CLI alternative.
- A conceptual workflow illustration showing AI-managed queueing, human approval, local safety checks, and one-at-a-time execution.
- Source-checkout quick-start, compatibility, security, support, contribution, and operating documentation.
- Local and GitHub CI checks for compilation, Python 3.9/3.11/3.13 behavior, JSON contracts, Markdown links, and tracked-file and history safety.

### Safety notes

- The scheduler does not guarantee billing outcomes or predict final turn usage.
- Missing, stale, malformed, or conflicting account, quota, credit, integrity, capability, lease, or recovery evidence fails closed.
- The public alpha adds no network listener, external notification, new authentication path, reset-credit use, paid-credit fallback, package publication, or launchd activation.
