"""Strict simulation, guarded-canary, and approved-dispatch contract validation.

The repository publishes JSON Schema files for interoperable consumers.  The
runtime uses matching explicit validators so it stays within Python's standard
library and rejects unknown fields.
"""

import math
import re
from pathlib import PurePath
from typing import Any, Dict, Iterable, Mapping

from .constants import (
    FIVE_HOUR_MINUTES,
    LOCAL_DRY_RUN_CAPABILITIES,
    SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    WEEKLY_MINUTES,
)
from .errors import SchedulerError

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_DEFAULT_QUOTA_GUARD_CONFIG = {
    "enabled": False,
    "min_check_interval_seconds": 60,
    "max_check_interval_seconds": 3600,
    "max_targets": 100,
    "resume_hysteresis_percent": 1.0,
}


def _object(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SchedulerError("SCHEMA_INVALID", "%s must be an object" % name)
    return dict(value)


def _exact_keys(
    value: Mapping[str, Any],
    name: str,
    required: Iterable[str],
    optional: Iterable[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    unknown = sorted(set(value) - allowed)
    if missing or unknown:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "%s has invalid fields" % name,
            details={"missing": missing, "unknown": unknown},
        )


def _version(value: Mapping[str, Any], name: str) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise SchedulerError(
            "SCHEMA_UNSUPPORTED",
            "%s uses an unsupported schema version" % name,
            details={"supported": [SCHEMA_VERSION]},
        )


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchedulerError("SCHEMA_INVALID", "%s must be a number" % name)
    result = float(value)
    if not math.isfinite(result) or result < low or result > high:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "%s is outside its permitted range" % name,
            details={"maximum": high, "minimum": low},
        )
    return result


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or value > high:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "%s must be an integer in range" % name,
            details={"maximum": high, "minimum": low},
        )
    return value


def _string(value: Any, name: str, *, identifier: bool = False, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SchedulerError("SCHEMA_INVALID", "%s must be a non-empty string" % name)
    if identifier and not _IDENTIFIER.match(value):
        raise SchedulerError("SCHEMA_INVALID", "%s is not a valid identifier" % name)
    return value


def validate_identifier(value: Any, name: str) -> str:
    return _string(value, name, identifier=True)


def validate_policy(value: Any) -> Dict[str, Any]:
    data = _object(value, "policy")
    _exact_keys(
        data,
        "policy",
        {
            "schema_version",
            "five_hour_reserve_percent",
            "weekly_reserve_percent",
            "estimate_multiplier",
            "max_snapshot_age_seconds",
            "lease_seconds",
            "max_concurrency",
        },
    )
    _version(data, "policy")
    result = {
        "schema_version": SCHEMA_VERSION,
        "five_hour_reserve_percent": _number(
            data["five_hour_reserve_percent"], "five_hour_reserve_percent", 0.0, 100.0
        ),
        "weekly_reserve_percent": _number(
            data["weekly_reserve_percent"], "weekly_reserve_percent", 0.0, 100.0
        ),
        "estimate_multiplier": _number(
            data["estimate_multiplier"], "estimate_multiplier", 1.0, 10.0
        ),
        "max_snapshot_age_seconds": _integer(
            data["max_snapshot_age_seconds"], "max_snapshot_age_seconds", 1, 3600
        ),
        "lease_seconds": _integer(data["lease_seconds"], "lease_seconds", 5, 3600),
        "max_concurrency": _integer(data["max_concurrency"], "max_concurrency", 1, 1),
    }
    return result


def validate_config(value: Any) -> Dict[str, Any]:
    data = _object(value, "configuration")
    _exact_keys(
        data,
        "configuration",
        {
            "schema_version",
            "dry_run",
            "database_path",
            "profile_key",
            "limit_id",
            "capabilities",
            "policy",
            "workspace_roots",
            "live_test",
        },
        {"background", "dispatch", "quota_guard"},
    )
    _version(data, "configuration")
    if data["dry_run"] is not True:
        raise SchedulerError("DRY_RUN_REQUIRED", "The general work controller requires dry_run to be true")
    capabilities = data["capabilities"]
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise SchedulerError("SCHEMA_INVALID", "capabilities must be an array of strings")
    if len(capabilities) != len(set(capabilities)):
        raise SchedulerError("SCHEMA_INVALID", "capabilities must not contain duplicates")
    unsupported = sorted(set(capabilities) - LOCAL_DRY_RUN_CAPABILITIES)
    if unsupported:
        raise SchedulerError(
            "CAPABILITY_DENIED",
            "The configuration requests unsupported capabilities",
            details={"unsupported": unsupported},
        )
    database_path = _string(data["database_path"], "database_path", maximum=1024)
    if database_path != ":memory:":
        parsed_path = PurePath(database_path)
        if parsed_path.is_absolute() or ".." in parsed_path.parts:
            raise SchedulerError(
                "PATH_DENIED",
                "database_path must stay within the configuration directory",
            )
    workspace_roots = data["workspace_roots"]
    if not isinstance(workspace_roots, list) or not workspace_roots:
        raise SchedulerError("SCHEMA_INVALID", "workspace_roots must be a non-empty array")
    normalized_roots = [
        _string(item, "workspace_roots item", maximum=1024) for item in workspace_roots
    ]
    if len(normalized_roots) != len(set(normalized_roots)):
        raise SchedulerError("SCHEMA_INVALID", "workspace_roots must not contain duplicates")
    live_test = validate_live_test_config(data["live_test"])
    dispatch = validate_dispatch_config(
        data.get(
            "dispatch",
            {
                "enabled": False,
                "poll_interval_seconds": 5,
                "require_paid_credits_unavailable": True,
                "deny_hooks": True,
                "deny_mcp_servers": True,
                "deny_apps": True,
            },
        )
    )
    background = validate_background_config(
        data.get(
            "background",
            {
                "enabled": False,
                "poll_interval_seconds": 30,
                "max_backoff_seconds": 300,
                "jitter_ratio": 0.2,
                "service_lease_seconds": 600,
                "notification_path": ".scheduler/notifications.jsonl",
            },
        )
    )
    quota_guard = validate_quota_guard_config(
        data.get("quota_guard", dict(_DEFAULT_QUOTA_GUARD_CONFIG))
    )
    if quota_guard["enabled"]:
        required_guard_capabilities = {
            "quota_guard.read",
            "quota_guard.local.write",
            "quota_guard.thread.control",
        }
        missing_guard_capabilities = sorted(required_guard_capabilities - set(capabilities))
        if missing_guard_capabilities:
            raise SchedulerError(
                "CAPABILITY_DENIED",
                "Enabled quota guard requires its fixed local capabilities",
                details={"missing": missing_guard_capabilities},
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "dry_run": True,
        "database_path": database_path,
        "profile_key": _string(data["profile_key"], "profile_key", identifier=True),
        "limit_id": _string(data["limit_id"], "limit_id", identifier=True),
        "capabilities": sorted(capabilities),
        "policy": validate_policy(data["policy"]),
        "workspace_roots": normalized_roots,
        "live_test": live_test,
        "dispatch": dispatch,
        "background": background,
        "quota_guard": quota_guard,
    }


def validate_quota_guard_config(value: Any) -> Dict[str, Any]:
    data = _object(value, "quota_guard")
    _exact_keys(
        data,
        "quota_guard",
        {
            "enabled",
            "min_check_interval_seconds",
            "max_check_interval_seconds",
            "max_targets",
            "resume_hysteresis_percent",
        },
    )
    if not isinstance(data["enabled"], bool):
        raise SchedulerError("SCHEMA_INVALID", "quota_guard.enabled must be a boolean")
    min_interval = _integer(
        data["min_check_interval_seconds"],
        "quota_guard.min_check_interval_seconds",
        60,
        3600,
    )
    max_interval = _integer(
        data["max_check_interval_seconds"],
        "quota_guard.max_check_interval_seconds",
        60,
        86400,
    )
    if max_interval < min_interval:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "quota_guard.max_check_interval_seconds must be at least the minimum",
        )
    return {
        "enabled": data["enabled"],
        "min_check_interval_seconds": min_interval,
        "max_check_interval_seconds": max_interval,
        "max_targets": _integer(data["max_targets"], "quota_guard.max_targets", 1, 100),
        "resume_hysteresis_percent": _number(
            data["resume_hysteresis_percent"],
            "quota_guard.resume_hysteresis_percent",
            0.1,
            20.0,
        ),
    }


def _quota_guard_plan_limits(
    config: Any = None, *, limits: Any = None
) -> Dict[str, int]:
    if config is not None and limits is not None:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "Provide either config or limits, not both",
        )
    supplied = limits if limits is not None else config
    if supplied is None:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "quota_guard plan validation requires config or limits",
        )
    if not isinstance(supplied, Mapping):
        raise SchedulerError("SCHEMA_INVALID", "quota_guard config or limits must be an object")

    # Callers may pass the complete validated configuration, its quota_guard
    # section, or a focused limits mapping.  A legacy complete configuration
    # has no quota_guard section, so it receives the same safe defaults as
    # validate_config.
    if "quota_guard" in supplied:
        supplied = supplied["quota_guard"]
    elif (
        "schema_version" in supplied
        and "capabilities" in supplied
        and not {
            "min_check_interval_seconds",
            "max_check_interval_seconds",
            "max_targets",
        }.intersection(supplied)
    ):
        supplied = _DEFAULT_QUOTA_GUARD_CONFIG
    if not isinstance(supplied, Mapping):
        raise SchedulerError("SCHEMA_INVALID", "quota_guard limits must be an object")

    min_interval = _integer(
        supplied.get("min_check_interval_seconds"),
        "quota_guard.min_check_interval_seconds",
        60,
        3600,
    )
    max_interval = _integer(
        supplied.get("max_check_interval_seconds"),
        "quota_guard.max_check_interval_seconds",
        60,
        86400,
    )
    if max_interval < min_interval:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "quota_guard.max_check_interval_seconds must be at least the minimum",
        )
    max_targets = _integer(
        supplied.get("max_targets"), "quota_guard.max_targets", 1, 100
    )
    return {
        "min_check_interval_seconds": min_interval,
        "max_check_interval_seconds": max_interval,
        "max_targets": max_targets,
    }


def validate_quota_guard_plan(
    value: Any, config: Any = None, *, limits: Any = None
) -> Dict[str, Any]:
    data = _object(value, "quota_guard_plan")
    _exact_keys(
        data,
        "quota_guard_plan",
        {
            "schema_version",
            "threshold_remaining_percent",
            "check_interval_seconds",
            "target_thread_ids",
            "resume_non_goal_threads",
        },
    )
    _version(data, "quota_guard_plan")
    plan_limits = _quota_guard_plan_limits(config, limits=limits)

    target_thread_ids = data["target_thread_ids"]
    if not isinstance(target_thread_ids, list):
        raise SchedulerError(
            "SCHEMA_INVALID", "quota_guard_plan.target_thread_ids must be an array"
        )
    if not 1 <= len(target_thread_ids) <= plan_limits["max_targets"]:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "quota_guard_plan.target_thread_ids count is outside its permitted range",
            details={"maximum": plan_limits["max_targets"], "minimum": 1},
        )
    normalized_targets = [
        validate_identifier(item, "quota_guard_plan target_thread_id")
        for item in target_thread_ids
    ]
    if len(normalized_targets) != len(set(normalized_targets)):
        raise SchedulerError(
            "SCHEMA_INVALID",
            "quota_guard_plan.target_thread_ids must not contain duplicates",
        )
    if not isinstance(data["resume_non_goal_threads"], bool):
        raise SchedulerError(
            "SCHEMA_INVALID",
            "quota_guard_plan.resume_non_goal_threads must be a boolean",
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "threshold_remaining_percent": _number(
            data["threshold_remaining_percent"],
            "quota_guard_plan.threshold_remaining_percent",
            0.0,
            100.0,
        ),
        "check_interval_seconds": _integer(
            data["check_interval_seconds"],
            "quota_guard_plan.check_interval_seconds",
            plan_limits["min_check_interval_seconds"],
            plan_limits["max_check_interval_seconds"],
        ),
        "target_thread_ids": sorted(normalized_targets),
        "resume_non_goal_threads": data["resume_non_goal_threads"],
    }


def validate_background_config(value: Any) -> Dict[str, Any]:
    data = _object(value, "background")
    _exact_keys(
        data,
        "background",
        {
            "enabled",
            "poll_interval_seconds",
            "max_backoff_seconds",
            "jitter_ratio",
            "service_lease_seconds",
            "notification_path",
        },
    )
    if not isinstance(data["enabled"], bool):
        raise SchedulerError("SCHEMA_INVALID", "background.enabled must be a boolean")
    poll_interval = _integer(
        data["poll_interval_seconds"], "background.poll_interval_seconds", 5, 3600
    )
    max_backoff = _integer(
        data["max_backoff_seconds"], "background.max_backoff_seconds", 5, 3600
    )
    service_lease = _integer(
        data["service_lease_seconds"], "background.service_lease_seconds", 30, 7200
    )
    if max_backoff < poll_interval:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "background.max_backoff_seconds must be at least the poll interval",
        )
    if service_lease <= max_backoff:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "background.service_lease_seconds must exceed the maximum backoff",
        )
    notification_path = _string(
        data["notification_path"], "background.notification_path", maximum=1024
    )
    parsed_path = PurePath(notification_path)
    if parsed_path.is_absolute() or ".." in parsed_path.parts:
        raise SchedulerError(
            "PATH_DENIED",
            "background.notification_path must stay within the configuration directory",
        )
    return {
        "enabled": data["enabled"],
        "poll_interval_seconds": poll_interval,
        "max_backoff_seconds": max_backoff,
        "jitter_ratio": _number(
            data["jitter_ratio"], "background.jitter_ratio", 0.0, 0.5
        ),
        "service_lease_seconds": service_lease,
        "notification_path": notification_path,
    }


def validate_dispatch_config(value: Any) -> Dict[str, Any]:
    data = _object(value, "dispatch")
    _exact_keys(
        data,
        "dispatch",
        {
            "enabled",
            "poll_interval_seconds",
            "require_paid_credits_unavailable",
            "deny_hooks",
            "deny_mcp_servers",
            "deny_apps",
        },
        {"credit_verification_mode"},
    )
    if not isinstance(data["enabled"], bool):
        raise SchedulerError("SCHEMA_INVALID", "dispatch.enabled must be a boolean")
    for key in (
        "require_paid_credits_unavailable",
        "deny_hooks",
        "deny_mcp_servers",
        "deny_apps",
    ):
        if data[key] is not True:
            raise SchedulerError("SCHEMA_INVALID", "dispatch.%s must be true" % key)
    credit_verification_mode = data.get("credit_verification_mode", "machine_only")
    if credit_verification_mode not in {
        "machine_only",
        "operator_attested_subscription_only",
    }:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "dispatch.credit_verification_mode is invalid",
        )
    return {
        "enabled": data["enabled"],
        "poll_interval_seconds": _integer(
            data["poll_interval_seconds"], "dispatch.poll_interval_seconds", 1, 30
        ),
        "require_paid_credits_unavailable": True,
        "credit_verification_mode": credit_verification_mode,
        "deny_hooks": True,
        "deny_mcp_servers": True,
        "deny_apps": True,
    }


def validate_live_test_config(value: Any) -> Dict[str, Any]:
    data = _object(value, "live_test")
    _exact_keys(
        data,
        "live_test",
        {
            "enabled",
            "model",
            "effort",
            "max_runtime_seconds",
            "poll_interval_seconds",
            "expected_usage",
            "require_paid_credits_unavailable",
        },
    )
    if not isinstance(data["enabled"], bool):
        raise SchedulerError("SCHEMA_INVALID", "live_test.enabled must be a boolean")
    if data["require_paid_credits_unavailable"] is not True:
        raise SchedulerError(
            "SCHEMA_INVALID",
            "live_test.require_paid_credits_unavailable must be true",
        )
    return {
        "enabled": data["enabled"],
        "model": _string(data["model"], "live_test.model", maximum=128),
        "effort": _string(data["effort"], "live_test.effort", identifier=True),
        "max_runtime_seconds": _integer(
            data["max_runtime_seconds"], "live_test.max_runtime_seconds", 10, 300
        ),
        "poll_interval_seconds": _integer(
            data["poll_interval_seconds"], "live_test.poll_interval_seconds", 1, 30
        ),
        "expected_usage": validate_usage(data["expected_usage"], "live_test.expected_usage"),
        "require_paid_credits_unavailable": True,
    }


def validate_usage(value: Any, name: str = "expected_usage") -> Dict[str, float]:
    data = _object(value, name)
    _exact_keys(data, name, {"five_hour_percent", "weekly_percent"})
    return {
        "five_hour_percent": _number(
            data["five_hour_percent"], "%s.five_hour_percent" % name, 0.0, 100.0
        ),
        "weekly_percent": _number(
            data["weekly_percent"], "%s.weekly_percent" % name, 0.0, 100.0
        ),
    }


def validate_job(value: Any) -> Dict[str, Any]:
    data = _object(value, "job")
    _exact_keys(
        data,
        "job",
        {"schema_version", "job_id", "work_ref", "priority", "expected_usage"},
        {"simulation"},
    )
    _version(data, "job")
    result = {
        "schema_version": SCHEMA_VERSION,
        "job_id": _string(data["job_id"], "job_id", identifier=True),
        "work_ref": _string(data["work_ref"], "work_ref", identifier=True),
        "priority": _integer(data["priority"], "priority", 0, 100),
        "expected_usage": validate_usage(data["expected_usage"]),
    }
    if "simulation" in data:
        simulation = _object(data["simulation"], "simulation")
        _exact_keys(simulation, "simulation", {"outcome", "actual_usage"})
        if simulation["outcome"] not in {"success", "failure"}:
            raise SchedulerError("SCHEMA_INVALID", "simulation.outcome is invalid")
        result["simulation"] = {
            "outcome": simulation["outcome"],
            "actual_usage": validate_usage(simulation["actual_usage"], "simulation.actual_usage"),
        }
    return result


def validate_work_package(value: Any) -> Dict[str, Any]:
    data = _object(value, "work_package")
    _exact_keys(
        data,
        "work_package",
        {
            "schema_version",
            "job",
            "objective",
            "execution",
            "dependencies",
            "not_before",
        },
    )
    _version(data, "work_package")
    job = validate_job(data["job"])
    if "simulation" in job:
        raise SchedulerError(
            "SCHEMA_INVALID", "A proposed work package cannot contain simulation output"
        )
    execution = _object(data["execution"], "work_package.execution")
    _exact_keys(
        execution,
        "work_package.execution",
        {"cwd", "model", "effort", "sandbox", "max_runtime_seconds"},
    )
    if execution["sandbox"] not in {"read_only", "workspace_write"}:
        raise SchedulerError("SCHEMA_INVALID", "work_package.execution.sandbox is invalid")
    dependencies = data["dependencies"]
    if not isinstance(dependencies, list):
        raise SchedulerError("SCHEMA_INVALID", "work_package.dependencies must be an array")
    normalized_dependencies = [
        _string(item, "work_package dependency", identifier=True) for item in dependencies
    ]
    if len(normalized_dependencies) != len(set(normalized_dependencies)):
        raise SchedulerError("SCHEMA_INVALID", "work_package.dependencies must not contain duplicates")
    if job["job_id"] in normalized_dependencies:
        raise SchedulerError("SCHEMA_INVALID", "A work package cannot depend on itself")
    not_before = data["not_before"]
    if not_before is not None:
        not_before = _number(not_before, "work_package.not_before", 0.0, 32_503_680_000.0)
    return {
        "schema_version": SCHEMA_VERSION,
        "job": job,
        "objective": _string(data["objective"], "work_package.objective", maximum=8000),
        "execution": {
            "cwd": _string(execution["cwd"], "work_package.execution.cwd", maximum=1024),
            "model": _string(execution["model"], "work_package.execution.model", maximum=128),
            "effort": _string(execution["effort"], "work_package.execution.effort", identifier=True),
            "sandbox": execution["sandbox"],
            "max_runtime_seconds": _integer(
                execution["max_runtime_seconds"],
                "work_package.execution.max_runtime_seconds",
                10,
                86_400,
            ),
        },
        "dependencies": normalized_dependencies,
        "not_before": not_before,
    }


def validate_approval(value: Any, *, now: float, allow_expired: bool = False) -> Dict[str, Any]:
    data = _object(value, "approval")
    _exact_keys(
        data,
        "approval",
        {
            "schema_version",
            "approval_id",
            "actor",
            "action",
            "scope_hash",
            "capabilities",
            "granted_at",
            "expires_at",
        },
    )
    _version(data, "approval")
    action = _string(data["action"], "approval.action", identifier=True)
    capabilities = data["capabilities"]
    if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
        raise SchedulerError("SCHEMA_INVALID", "approval.capabilities must be an array of strings")
    if set(capabilities) - LOCAL_DRY_RUN_CAPABILITIES:
        raise SchedulerError("CAPABILITY_DENIED", "The approval contains unsupported capabilities")
    granted_at = _number(data["granted_at"], "approval.granted_at", 0.0, 32_503_680_000.0)
    expires_at = _number(data["expires_at"], "approval.expires_at", 0.0, 32_503_680_000.0)
    if not allow_expired and granted_at > now + 60:
        raise SchedulerError("APPROVAL_INVALID", "The approval grant time is in the future")
    if expires_at <= granted_at:
        raise SchedulerError("APPROVAL_INVALID", "The approval expiry must follow its grant time")
    if not allow_expired and expires_at <= now:
        raise SchedulerError("APPROVAL_EXPIRED", "The approval is expired")
    scope_hash = _string(data["scope_hash"], "approval.scope_hash", maximum=64)
    if len(scope_hash) != 64 or any(char not in "0123456789abcdef" for char in scope_hash):
        raise SchedulerError("SCHEMA_INVALID", "approval.scope_hash must be a SHA-256 hex digest")
    return {
        "schema_version": SCHEMA_VERSION,
        "approval_id": _string(data["approval_id"], "approval_id", identifier=True),
        "actor": _string(data["actor"], "approval.actor", identifier=True),
        "action": action,
        "scope_hash": scope_hash,
        "capabilities": sorted(set(capabilities)),
        "granted_at": granted_at,
        "expires_at": expires_at,
    }


def require_approval(
    approval: Dict[str, Any],
    *,
    action: str,
    scope_hash: str,
    capability: str,
) -> None:
    if approval["action"] != action:
        raise SchedulerError("APPROVAL_SCOPE_MISMATCH", "The approval action does not match")
    if approval["scope_hash"] != scope_hash:
        raise SchedulerError("APPROVAL_SCOPE_MISMATCH", "The approval scope does not match")
    if capability not in approval["capabilities"]:
        raise SchedulerError("APPROVAL_SCOPE_MISMATCH", "The approval lacks the required capability")


def validate_window(value: Any, name: str, expected_minutes: int) -> Dict[str, Any]:
    data = _object(value, name)
    _exact_keys(
        data,
        name,
        {"window_duration_minutes", "used_percent", "remaining_percent", "resets_at"},
    )
    duration = _integer(data["window_duration_minutes"], "%s.window_duration_minutes" % name, 1, 100_000)
    if duration != expected_minutes:
        raise SchedulerError("SIGNAL_INVALID", "%s has an unexpected duration" % name)
    used = _number(data["used_percent"], "%s.used_percent" % name, 0.0, 100.0)
    remaining = _number(data["remaining_percent"], "%s.remaining_percent" % name, 0.0, 100.0)
    if abs((100.0 - used) - remaining) > 0.000001:
        raise SchedulerError("SIGNAL_INVALID", "%s remaining percentage is inconsistent" % name)
    resets_at = _number(data["resets_at"], "%s.resets_at" % name, 0.0, 32_503_680_000.0)
    return {
        "window_duration_minutes": duration,
        "used_percent": used,
        "remaining_percent": remaining,
        "resets_at": resets_at,
    }


def validate_snapshot(value: Any) -> Dict[str, Any]:
    data = _object(value, "quota_snapshot")
    _exact_keys(
        data,
        "quota_snapshot",
        {
            "schema_version",
            "observed_at",
            "source",
            "profile_key",
            "limit_id",
            "account_fingerprint",
            "account_type",
            "plan_type",
            "credit_signal",
            "paid_credit_state",
            "spend_control_state",
            "rate_limit_reached_type",
            "five_hour",
            "weekly",
        },
    )
    if data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise SchedulerError(
            "SCHEMA_UNSUPPORTED",
            "quota_snapshot uses an unsupported schema version",
            details={"supported": [SNAPSHOT_SCHEMA_VERSION]},
        )
    if data["source"] not in {"codex_app_server", "fixture"}:
        raise SchedulerError("SCHEMA_INVALID", "quota_snapshot.source is invalid")
    plan_type = data["plan_type"]
    if plan_type is not None:
        plan_type = _string(plan_type, "plan_type", maximum=64)
    reached = data["rate_limit_reached_type"]
    if reached is not None:
        reached = _string(reached, "rate_limit_reached_type", maximum=64)
    if data["credit_signal"] not in {"absent", "present", "unknown"}:
        raise SchedulerError("SCHEMA_INVALID", "credit_signal is invalid")
    if data["paid_credit_state"] not in {"available", "unavailable", "unknown"}:
        raise SchedulerError("SCHEMA_INVALID", "paid_credit_state is invalid")
    if data["spend_control_state"] not in {"reached", "not_reached", "unknown"}:
        raise SchedulerError("SCHEMA_INVALID", "spend_control_state is invalid")
    expected_credit_signal = {
        "available": "present",
        "unavailable": "absent",
        "unknown": "unknown",
    }[data["paid_credit_state"]]
    if data["credit_signal"] != expected_credit_signal:
        raise SchedulerError("SIGNAL_INVALID", "The paid-credit fields are inconsistent")
    account_fingerprint = _string(
        data["account_fingerprint"], "account_fingerprint", maximum=64
    )
    if len(account_fingerprint) != 64 or any(
        char not in "0123456789abcdef" for char in account_fingerprint
    ):
        raise SchedulerError(
            "SCHEMA_INVALID", "account_fingerprint must be a 64-character hex digest"
        )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "observed_at": _number(data["observed_at"], "observed_at", 0.0, 32_503_680_000.0),
        "source": data["source"],
        "profile_key": _string(data["profile_key"], "profile_key", identifier=True),
        "limit_id": _string(data["limit_id"], "limit_id", identifier=True),
        "account_fingerprint": account_fingerprint,
        "account_type": _string(data["account_type"], "account_type", identifier=True),
        "plan_type": plan_type,
        "credit_signal": data["credit_signal"],
        "paid_credit_state": data["paid_credit_state"],
        "spend_control_state": data["spend_control_state"],
        "rate_limit_reached_type": reached,
        "five_hour": validate_window(data["five_hour"], "five_hour", FIVE_HOUR_MINUTES),
        "weekly": validate_window(data["weekly"], "weekly", WEEKLY_MINUTES),
    }


def ensure_profile(snapshot: Dict[str, Any], config: Dict[str, Any]) -> None:
    limit_mismatch = config["limit_id"] != "auto" and snapshot["limit_id"] != config["limit_id"]
    if snapshot["profile_key"] != config["profile_key"] or limit_mismatch:
        raise SchedulerError("PROFILE_MISMATCH", "The quota snapshot belongs to another profile")
