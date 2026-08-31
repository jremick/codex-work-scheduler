# Background service operating model

Status: built and testable, not activated.

This phase adds a local foreground supervisor. A user LaunchAgent can invoke the same foreground command after a separate activation approval. The repository does not install, load, start, or enable a LaunchAgent. The example configuration keeps `background.enabled` set to `false`.

## Safety boundary

The supervisor does not create approval. It can dispatch only an immutable stored work package that already has a consumed approval with the `work.dispatch` capability. It reuses the existing transactional selector and App Server runner. It has no alternate prompt, model, path, sandbox, quota, credential, or tool surface.

The following conditions block dispatch:

- The controller is not `READY`.
- Either quota window is missing, stale, malformed, expired, or below its guarded 10% reserve.
- The account fingerprint, account type, plan, or selected limit bucket changes.
- Paid credits are explicitly available, spend control is reached, or the configured credit-verification policy rejects the signal.
- The SQLite integrity check or audit chain fails.
- A run, simulation lease, or supervisor lease conflicts.
- A previous run has an uncertain outcome.
- The local notification sink cannot accept a redacted event.

`operator_attested_subscription_only` keeps its existing narrow meaning. It accepts unknown credit metadata only for the already bound subscription account. It never overrides explicit credit availability, a reached spend control, an account change, a reserve failure, or a missing quota window.

## Runtime components

`codex_work_scheduler.background.BackgroundSupervisor` owns the foreground loop. It wakes, renews its singleton service lease, checks database and audit integrity, reconciles expired claims to `needs_review`, refreshes quota, evaluates the queue, and dispatches at most one work package per cycle.

`codex_work_scheduler.notifications.NotificationBus` stores a deduplicated redacted event before delivery. `LocalJsonlSink` appends the same event to an owner-only JSONL file. The event types are `dispatch`, `completion`, `hold`, `safety_stop`, `signal_loss`, and `needs_review`. The event contract excludes objectives, prompts, task output, raw account identity, credentials, App Server stderr, and exact quota percentages.

SQLite remains the operational source of truth. The existing approved work-package record necessarily contains the immutable objective used for dispatch. The new service lease, audit events, notification events, launchd output, and foreground results do not copy that objective or task output.

## Polling and backoff

The normal interval is `background.poll_interval_seconds`. Probe failures use exponential backoff:

```text
min(max_backoff, poll_interval * 2^(consecutive_failures - 1))
```

The supervisor applies bounded symmetric jitter from `background.jitter_ratio`. The final delay never exceeds `background.max_backoff_seconds`. A successful, valid quota read resets the probe-failure count. A successful read does not resume a blocked controller. Resume still requires the existing scoped human approval.

The service lease must be longer than the maximum backoff. The supervisor renews it before each cycle and during active work safety checks. A second process cannot acquire an unexpired lease. A process can take over an expired service lease, but it never takes over or restarts an uncertain work run.

## Crash recovery and retry policy

At startup, expired simulation leases and expired `starting` or `running` work claims move to `needs_review`. The linked job also moves to `needs_review`, and the controller becomes `PAUSED`. Recovery emits an audit event and a local `needs_review` notification.

A work package that reaches a live dispatch claim can finish only as `succeeded` or `needs_review`. A runner failure, safety interruption, signal loss, unconfirmed terminal state, or service-level dispatch exception cannot return it to an automatically retryable queue state. Further work requires operator inspection and a new approved package when appropriate.

## Foreground commands

All commands return the existing deterministic JSON envelope.

```bash
python3 -m codex_work_scheduler --config scheduler.example.json service status
python3 -m codex_work_scheduler --config scheduler.example.json notifications list --limit 50
```

`service status` is read-only. It reports configuration and the redacted service-lease state. It does not probe quota or start the service.

The following commands require `background.enabled: true`. Do not run them against the local operating configuration without the activation approval described below.

```bash
python3 -m codex_work_scheduler --config <approved-config-path> service once
python3 -m codex_work_scheduler --config <approved-config-path> service run
```

`service once` runs one cycle and exits without waiting for the next cadence. `service run` stays in the foreground. `SIGTERM` and `SIGINT` request graceful shutdown. If a turn is active, its next safety check requests `turn/interrupt`; the package then requires review. `pause` stops new dispatch but keeps polling. `stop` causes an idle foreground service to exit cleanly and makes an active run fail its next safety check.

## LaunchAgent staging

The reviewed template is `launchd/io.github.jremick.codex-work-scheduler.plist.template`. Its public reverse-DNS label follows the GitHub repository namespace. It uses an absolute Python executable, the repository as `WorkingDirectory`, an explicit bounded `PATH` that contains the resolved Codex executable directory, `ProcessType=Background`, an owner-only umask, a 30-second exit timeout, and a 60-second launchd throttle.

The plist is disabled by default. `KeepAlive.SuccessfulExit=false` restarts an unexpected non-zero exit but does not restart a clean operator stop. `RunAtLoad=true` takes effect only after the separately approved install and enable steps.

Render only to a staging path:

```bash
python3 -m codex_work_scheduler --config <absolute-config-path> launchd render \
  --output <absolute-staging-path>/io.github.jremick.codex-work-scheduler.plist
python3 -m codex_work_scheduler --config <absolute-config-path> launchd check \
  --plist <absolute-staging-path>/io.github.jremick.codex-work-scheduler.plist
plutil -lint <absolute-staging-path>/io.github.jremick.codex-work-scheduler.plist
```

The renderer rejects output under user or system LaunchAgents and LaunchDaemons directories. There is no install, bootstrap, enable, kickstart, or uninstall command in this repository.

## Bounded activation canary

The exact next approval should name one configuration file and, if dispatch is intended, one already-approved job ID and package hash. Recommended approval text:

> Approve one bounded local foreground canary for Codex Work Scheduler. Change `background.enabled` to `true` only in `<absolute-config-path>`, verify the controller and audit state, and run `service once` once. It may perform current read-only account and quota probes, write redacted local SQLite/audit/notification state, and dispatch at most the already-approved job `<job-id>` with package hash `<package-hash>` if every existing gate passes. Do not install or load launchd, create credentials, change authentication, use paid credits, send external notifications, open a listener, retry failed or uncertain work, or dispatch any other job. Stop and report on any hold, safety stop, signal loss, notification failure, or review state.

If the canary must not dispatch work, state that no approved job may be present and keep the controller `PAUSED`. That canary proves wake, probe, hold, notification, and clean-exit behavior only.

LaunchAgent activation needs another approval after the foreground canary passes. That approval must authorize the exact plist destination under the current user's `~/Library/LaunchAgents`, the `launchctl bootstrap`, `enable`, and `kickstart` operations, and the expected continuous local impact.

## Activation preflight and rollback

Before any foreground or launchd activation:

1. Run the full test suite and plist checks from a clean checkout.
2. Read `service status`, controller `status`, `reconcile --dry-run`, and `audit verify`.
3. Confirm there is no active service lease, simulation lease, or run.
4. Confirm `background.enabled` is the only planned configuration activation change.
5. Confirm the local notification path is inside the configuration directory and owner-only.
6. If dispatch is allowed, confirm the exact stored package hash and `work.dispatch` authority.

Foreground rollback is `stop` followed by confirmation that the service lease is released. Restore `background.enabled` to `false` in the approved configuration copy.

LaunchAgent rollback is: stop the controller, boot out the exact user service, verify that it is absent, move the plist from `~/Library/LaunchAgents` to an owner-only backup path, restore `background.enabled` to `false`, then read back service, controller, run, reconciliation, audit, and notification state. Do not delete the SQLite database or uncertain Codex task. Any active or expired work claim remains a `needs_review` case.

## Verification sources

The implementation was checked against the installed `codex-cli 0.150.1` App Server JSON schemas and the official [Codex App Server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md). Queue dispatch continues to use a fresh stdio App Server and does not use the experimental websocket transport. The optional quota guard uses the fixed local `codex app-server proxy` command so task-control requests reach the already-running App Server control socket.

The launchd template follows the current local `launchd.plist(5)` contract and Apple's [Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html) guidance. A LaunchAgent runs in the logged-in user's context; this design does not add a system LaunchDaemon.

## Verification commands

```bash
python3 -m py_compile codex_work_scheduler/*.py
python3 -m unittest discover -s tests -v
python3 -m json.tool scheduler.example.json >/dev/null
for schema in schemas/*.json; do python3 -m json.tool "$schema" >/dev/null; done
git diff --check
```

The service tests inject probe failure, stale signals, account change, reserve failure, explicit credit availability, audit corruption, runner failure, notification failure, duplicate-process contention, lease expiry, process crash, operator pause/stop, and shutdown during an active run.
