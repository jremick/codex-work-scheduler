"""Fail-closed dual-window reserve policy."""

from typing import Any, Dict, List, Optional


SIGNAL_REASON_CODES = frozenset(
    {
        "SIGNAL_MISSING",
        "SIGNAL_STALE",
        "SIGNAL_INVALID",
        "PROFILE_MISMATCH",
        "ACCOUNT_IDENTITY_UNAVAILABLE",
        "RATE_LIMIT_REACHED",
    }
)


def missing_decision() -> Dict[str, Any]:
    return {
        "eligible": False,
        "estimate_kind": "estimate",
        "reasons": ["SIGNAL_MISSING"],
        "signal_status": "missing",
        "windows": {},
    }


def invalid_signal_decision(reason: str) -> Dict[str, Any]:
    return {
        "eligible": False,
        "estimate_kind": "estimate",
        "reasons": [reason],
        "signal_status": "invalid",
        "windows": {},
    }


def evaluate_policy(
    snapshot: Optional[Dict[str, Any]],
    policy: Dict[str, Any],
    *,
    now: float,
    expected_usage: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    if snapshot is None:
        return missing_decision()

    expected = expected_usage or {"five_hour_percent": 0.0, "weekly_percent": 0.0}
    reasons: List[str] = []
    age = now - float(snapshot["observed_at"])
    if age < -60:
        reasons.append("SIGNAL_INVALID")
    elif age > policy["max_snapshot_age_seconds"]:
        reasons.append("SIGNAL_STALE")

    if snapshot.get("rate_limit_reached_type"):
        reasons.append("RATE_LIMIT_REACHED")

    windows: Dict[str, Dict[str, Any]] = {}
    definitions = (
        (
            "five_hour",
            "five_hour_percent",
            policy["five_hour_reserve_percent"],
            "FIVE_HOUR_RESERVE",
        ),
        (
            "weekly",
            "weekly_percent",
            policy["weekly_reserve_percent"],
            "WEEKLY_RESERVE",
        ),
    )
    for window_name, usage_key, reserve, reason_code in definitions:
        window = snapshot[window_name]
        if window["resets_at"] <= now:
            reasons.append("SIGNAL_STALE")
        expected_percent = float(expected[usage_key])
        guarded_estimate = expected_percent * policy["estimate_multiplier"]
        post_run_remaining = window["remaining_percent"] - guarded_estimate
        safe = post_run_remaining >= reserve
        if not safe:
            reasons.append(reason_code)
        windows[window_name] = {
            "guarded_estimate_percent": guarded_estimate,
            "post_run_remaining_percent": post_run_remaining,
            "remaining_percent": window["remaining_percent"],
            "reserve_percent": reserve,
            "resets_at": window["resets_at"],
            "safe": safe,
        }

    reasons = sorted(set(reasons))
    signal_status = "fresh"
    if "SIGNAL_STALE" in reasons:
        signal_status = "stale"
    elif "SIGNAL_INVALID" in reasons:
        signal_status = "invalid"
    return {
        "credit_signal": snapshot["credit_signal"],
        "eligible": not reasons,
        "estimate_kind": "estimate",
        "reasons": reasons,
        "signal_age_seconds": age,
        "signal_status": signal_status,
        "windows": windows,
    }


def has_signal_failure(decision: Dict[str, Any]) -> bool:
    return any(reason in SIGNAL_REASON_CODES for reason in decision["reasons"])
