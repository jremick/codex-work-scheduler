# Changelog

All notable public changes will be recorded here. The project uses alpha releases and does not promise backward compatibility before beta.

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
