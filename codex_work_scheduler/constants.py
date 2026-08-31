"""Versioned constants and deny-by-default capability definitions."""

SCHEMA_VERSION = "1"
SNAPSHOT_SCHEMA_VERSION = "3"
APP_VERSION = "0.7.0-alpha.1"

FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 10_080

LOCAL_DRY_RUN_CAPABILITIES = frozenset(
    {
        "audit.read",
        "backlog.local.write",
        "background.read",
        "background.run",
        "control.read",
        "control.local.write",
        "launchd.render",
        "live_test.dispatch",
        "monitor.local.write",
        "monitor.read",
        "notification.local.write",
        "policy.read",
        "policy.evaluate",
        "policy.local.write",
        "queue.read",
        "queue.local.write",
        "quota.read",
        "quota_guard.read",
        "quota_guard.local.write",
        "quota_guard.thread.control",
        "reconcile.local",
        "simulate.local",
        "work.dispatch",
    }
)

LIVE_TEST_OUTBOUND_ALLOWLIST = frozenset(
    {
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
        "turn/interrupt",
    }
)

WORK_OUTBOUND_ALLOWLIST = frozenset(
    {
        "initialize",
        "initialized",
        "hooks/list",
        "app/installed",
        "mcpServerStatus/list",
        "experimentalFeature/list",
        "thread/start",
        "turn/start",
        "turn/interrupt",
    }
)

MONITOR_OUTBOUND_ALLOWLIST = frozenset(
    {
        "initialize",
        "initialized",
        "thread/read",
    }
)

QUOTA_GUARD_OUTBOUND_ALLOWLIST = frozenset(
    {
        "initialize",
        "initialized",
        "thread/list",
        "thread/read",
        "thread/goal/get",
        "thread/goal/set",
        "thread/resume",
        "turn/interrupt",
        "turn/start",
    }
)

# These are the only messages the production probe can send.  The tuple form is
# deliberate: callers cannot extend the allowlist through configuration.
APP_SERVER_OUTBOUND_ALLOWLIST = frozenset(
    {
        "initialize",
        "initialized",
        "account/read",
        "account/rateLimits/read",
    }
)

PROHIBITED_CAPABILITIES = frozenset(
    {
        "auth.modify",
        "background.install",
        "codex.turn.start",
        "credits.consume",
        "external.notify",
        "network.listen",
        "queue.execute",
    }
)

CONTROLLER_MODES = frozenset({"PAUSED", "READY", "STOPPED", "BLOCKED"})
JOB_STATES = frozenset(
    {
        "approved",
        "cancelled",
        "simulating",
        "simulated",
        "held_policy",
        "held_dependency",
        "held_schedule",
        "held_capability",
        "needs_review",
        "running",
        "succeeded",
        "failed",
        "interrupted",
    }
)
