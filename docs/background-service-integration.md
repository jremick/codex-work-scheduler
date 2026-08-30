# Background-service integration record

Status: **integrated for public-alpha review**

Integration date: **2026-08-30**

The foreground supervisor and launchd staging surface are integrated with the public documentation and CI suite. The public alpha does not claim a launchd installer or live LaunchAgent activation.

## Preserved safety boundaries

- The example configuration keeps `background.enabled: false`.
- The supervisor can select at most one already-approved immutable work package per cycle.
- Package, approval, quota, credit, integrity, lease, capability, workspace, and controller-state gates remain on the existing dispatch path.
- A durable singleton lease prevents concurrent supervisor ownership.
- Failed, interrupted, expired, or uncertain work moves to `needs_review`; it is not retried automatically.
- The only notification sink is owner-only local JSONL with SQLite delivery state. It excludes prompts, objectives, task output, raw account identity, exact quota values, credentials, and App Server stderr.
- The service does not create authentication, consume reset credits, enable paid-credit fallback, open a listener, or add external notifications.
- The launchd renderer writes only to staging, produces a disabled plist, and rejects LaunchAgents and LaunchDaemons destinations. It has no installation or activation command.
- The public label and template use `io.github.jremick.codex-work-scheduler`; no local home path, account identifier, credential, or runtime value is tracked.

## Covered failure cases

The local suite covers disabled startup, pause and stop, duplicate ownership, lease expiry, crash recovery, stale quota, account change, reserve failure, explicit paid-credit availability, corrupt audit state, runner failure, notification failure, shutdown during an active run, redaction, plist validation, and denied install destinations. Plist rendering uses a synthetic Codex executable, so CI does not require Codex installation or authentication.

## Release-candidate checks

Before visibility or an alpha prerelease:

- Run the full Python suite, compilation, JSON validation, local-link check, tracked-file and Git-history public-safety scan, and `git diff --check`.
- Render the plist with isolated temporary inputs, run the repository checker, and run `plutil -lint`.
- Run the source-checkout quick start from a fresh clone with no scheduler state.
- Recheck App Server request and response fields against current official Codex documentation.
- Obtain successful private GitHub Actions runs for the integrated commit and record the actual check names.
- Repeat the public-surface scan after any release-cut change.

Live foreground dispatch, launchd installation, repeated install, reboot startup, and launchctl rollback require separate local operational approval. They are not claims of the public alpha and are not required for a source-checkout release whose supervisor remains disabled by default.

## Stop conditions

Stop the release if the combined build can dispatch while not explicitly ready, start more than one turn, retry uncertain work, weaken a quota or credit gate, write outside the approved workspace, use task network access, alter authentication, leak private state, or leave service ownership uncertain after a crash.
