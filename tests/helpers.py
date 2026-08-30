import copy
from typing import Any, Dict

from codex_work_scheduler.constants import LOCAL_DRY_RUN_CAPABILITIES
from codex_work_scheduler.util import payload_hash


NOW = 2_000_000_000.0


class FixedClock:
    def __init__(self, value: float = NOW) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def policy(**overrides: Any) -> Dict[str, Any]:
    value = {
        "schema_version": "1",
        "five_hour_reserve_percent": 10.0,
        "weekly_reserve_percent": 10.0,
        "estimate_multiplier": 1.25,
        "max_snapshot_age_seconds": 300,
        "lease_seconds": 60,
        "max_concurrency": 1,
    }
    value.update(overrides)
    return value


def config(database_path: str, **overrides: Any) -> Dict[str, Any]:
    value = {
        "schema_version": "1",
        "dry_run": True,
        "database_path": database_path,
        "profile_key": "test-profile",
        "limit_id": "codex",
        "capabilities": sorted(LOCAL_DRY_RUN_CAPABILITIES),
        "policy": policy(),
        "workspace_roots": ["."],
        "live_test": {
            "enabled": True,
            "model": "gpt-5.6-luna",
            "effort": "low",
            "max_runtime_seconds": 30,
            "poll_interval_seconds": 2,
            "expected_usage": {
                "five_hour_percent": 0.5,
                "weekly_percent": 0.1,
            },
            "require_paid_credits_unavailable": True,
        },
        "dispatch": {
            "enabled": True,
            "poll_interval_seconds": 2,
            "require_paid_credits_unavailable": True,
            "credit_verification_mode": "machine_only",
            "deny_hooks": True,
            "deny_mcp_servers": True,
            "deny_apps": True,
        },
        "background": {
            "enabled": False,
            "poll_interval_seconds": 30,
            "max_backoff_seconds": 300,
            "jitter_ratio": 0.2,
            "service_lease_seconds": 600,
            "notification_path": ".scheduler/notifications.jsonl",
        },
    }
    value.update(overrides)
    return value


def snapshot(
    *,
    observed_at: float = NOW,
    five_used: float = 20.0,
    weekly_used: float = 30.0,
    five_reset: float = NOW + 3600,
    weekly_reset: float = NOW + 7 * 86400,
    source: str = "fixture",
    profile_key: str = "test-profile",
    plan_type: str = "pro",
    account_fingerprint: str = "1" * 64,
    account_type: str = "chatgpt",
) -> Dict[str, Any]:
    return {
        "schema_version": "3",
        "observed_at": observed_at,
        "source": source,
        "profile_key": profile_key,
        "limit_id": "codex",
        "account_fingerprint": account_fingerprint,
        "account_type": account_type,
        "plan_type": plan_type,
        "credit_signal": "absent",
        "paid_credit_state": "unavailable",
        "spend_control_state": "not_reached",
        "rate_limit_reached_type": None,
        "five_hour": {
            "window_duration_minutes": 300,
            "used_percent": five_used,
            "remaining_percent": 100.0 - five_used,
            "resets_at": five_reset,
        },
        "weekly": {
            "window_duration_minutes": 10080,
            "used_percent": weekly_used,
            "remaining_percent": 100.0 - weekly_used,
            "resets_at": weekly_reset,
        },
    }


def job(
    job_id: str = "job-1",
    *,
    priority: int = 50,
    five_expected: float = 5.0,
    weekly_expected: float = 1.0,
    actual_five: float = None,
    actual_weekly: float = None,
    outcome: str = "success",
) -> Dict[str, Any]:
    value = {
        "schema_version": "1",
        "job_id": job_id,
        "work_ref": "work-%s" % job_id,
        "priority": priority,
        "expected_usage": {
            "five_hour_percent": five_expected,
            "weekly_percent": weekly_expected,
        },
    }
    if actual_five is not None or actual_weekly is not None or outcome != "success":
        value["simulation"] = {
            "outcome": outcome,
            "actual_usage": {
                "five_hour_percent": five_expected if actual_five is None else actual_five,
                "weekly_percent": weekly_expected if actual_weekly is None else actual_weekly,
            },
        }
    return value


def work_package(
    job_id: str = "job-1",
    *,
    priority: int = 50,
    dependencies=None,
    not_before=None,
) -> Dict[str, Any]:
    return {
        "schema_version": "1",
        "job": job(job_id, priority=priority),
        "objective": "Perform the approved local work package.",
        "execution": {
            "cwd": ".",
            "model": "gpt-5.6-luna",
            "effort": "low",
            "sandbox": "workspace_write",
            "max_runtime_seconds": 600,
        },
        "dependencies": list(dependencies or []),
        "not_before": not_before,
    }


def approval(
    action: str,
    scoped_value: Dict[str, Any],
    capability: str,
    *,
    approval_id: str,
    now: float = NOW,
) -> Dict[str, Any]:
    return {
        "schema_version": "1",
        "approval_id": approval_id,
        "actor": "operator",
        "action": action,
        "scope_hash": payload_hash(scoped_value),
        "capabilities": [capability],
        "granted_at": now - 1,
        "expires_at": now + 600,
    }


class FakeProbe:
    def __init__(self, value: Dict[str, Any]) -> None:
        self.value = copy.deepcopy(value)
        self.calls = 0

    def read(
        self,
        *,
        profile_key: str,
        limit_id: str,
        account_fingerprint_key: str,
    ) -> Dict[str, Any]:
        self.calls += 1
        result = copy.deepcopy(self.value)
        result["profile_key"] = profile_key
        result["limit_id"] = limit_id
        return result
