# Compatibility

Codex Work Scheduler is alpha software. This table records the current evidence, not a long-term compatibility promise.

| Surface | Public-alpha position | Current evidence |
| --- | --- | --- |
| Operating system | macOS only | The runtime and optional launchd surface target local macOS operation. Other platforms are unverified. |
| Python | 3.9 or later | The complete suite passes locally on Python 3.9.6. Private GitHub CI passes on Python 3.9, 3.11, and 3.13. |
| Python dependencies | Standard library only | No third-party Python manifest or runtime dependency is present. |
| Codex CLI | App Server interface required | The implementation baseline is Codex CLI 0.150.1. Other CLI versions are unverified. |
| Distribution | Source checkout and GitHub alpha prerelease | No package, installer, or custom release artifact is published. |
| State | Local SQLite | Schema and migrations may change before beta. Back up state before updating. |
| Background service | Disabled by default | The foreground supervisor is covered by the fake-probe suite. The repository renders a disabled LaunchAgent plist to staging only; live install, reboot, and launchctl operation are not claimed. |
| Quota guard | Disabled by default | Synthetic App Server tests cover inventory, goal pause/resume, exact-turn interruption, reset proof, and durable recovery. Live control of a desktop-owned task is not yet claimed. |

## App Server interface review

On 2026-08-30, the implementation allowlists were checked against the current official [Codex App Server documentation](https://developers.openai.com/codex/app-server/):

- `account/read` and `account/rateLimits/read`
- `hooks/list`, `app/installed`, `mcpServerStatus/list`, and `experimentalFeature/list`
- `thread/start`, `thread/list`, `thread/read`, `thread/resume`, `turn/start`, and `turn/interrupt`
- `thread/goal/get` and status-only `thread/goal/set`
- `approvalPolicy`, `sandboxPolicy`, `workspaceWrite.writableRoots`, and `networkAccess`
- `rateLimitsByLimitId`, `usedPercent`, `windowDurationMins`, `resetsAt`, `planType`, `credits`, and `rateLimitReachedType`

The App Server is an evolving interface. Repeat this review at the public visibility cut and whenever the Codex CLI baseline changes. A documentation match does not replace the fake-server suite or a separately approved read-only live canary.

## Alpha compatibility policy

Configuration keys, JSON schemas, CLI commands, SQLite schema, service installation, and recovery behavior can change during alpha. Breaking changes must be called out in the README or release notes. The current store migrates schema version 1 to 2 for quota-guard state, but no general backward-compatibility window is promised before beta.
