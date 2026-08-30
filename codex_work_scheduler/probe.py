"""Strict read-only adapter for the Codex App Server stdio protocol."""

import json
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .constants import (
    APP_SERVER_OUTBOUND_ALLOWLIST,
    APP_VERSION,
    FIVE_HOUR_MINUTES,
    SNAPSHOT_SCHEMA_VERSION,
    WEEKLY_MINUTES,
)
from .errors import SchedulerError
from .util import keyed_fingerprint


class CodexAppServerProbe:
    """Fetches rate-limit state without exposing a configurable command surface."""

    COMMAND = ("codex", "app-server", "--stdio")

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._popen_factory = popen_factory
        self._clock = clock

    @staticmethod
    def build_messages() -> List[Dict[str, Any]]:
        messages = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex_work_scheduler",
                        "title": "Codex Work Scheduler",
                        "version": APP_VERSION,
                    }
                },
            },
            {"method": "initialized", "params": {}},
            {"id": 2, "method": "account/read", "params": {"refreshToken": False}},
            {"id": 3, "method": "account/rateLimits/read"},
        ]
        for message in messages:
            method = message["method"]
            if method not in APP_SERVER_OUTBOUND_ALLOWLIST:
                raise SchedulerError("METHOD_DENIED", "The App Server method is not allowlisted")
        return messages

    def read(
        self,
        *,
        profile_key: str,
        limit_id: str,
        account_fingerprint_key: str,
    ) -> Dict[str, Any]:
        try:
            process = self._popen_factory(
                list(self.COMMAND),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise SchedulerError(
                "PROBE_UNAVAILABLE",
                "The Codex App Server could not be started",
                retryable=True,
            ) from exc
        if process.stdin is None or process.stdout is None:
            self._terminate(process)
            raise SchedulerError("PROBE_UNAVAILABLE", "The App Server stdio transport is unavailable")

        output_queue: "queue.Queue[Any]" = queue.Queue()

        def read_output() -> None:
            try:
                for line in process.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=read_output, name="quota-probe-reader", daemon=True)
        reader.start()
        try:
            for message in self.build_messages():
                try:
                    process.stdin.write(json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n")
                    process.stdin.flush()
                except OSError as exc:
                    raise SchedulerError(
                        "PROBE_UNAVAILABLE",
                        "The App Server stdio transport closed unexpectedly",
                        retryable=True,
                    ) from exc

            deadline = self._clock() + self.timeout_seconds
            responses: Dict[int, Dict[str, Any]] = {}
            while self._clock() < deadline:
                remaining = max(0.01, deadline - self._clock())
                try:
                    line = output_queue.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, TypeError) as exc:
                    raise SchedulerError("PROBE_PROTOCOL_ERROR", "The App Server returned invalid JSON") from exc
                response_id = message.get("id")
                if response_id in {2, 3}:
                    responses[response_id] = message
                if 2 in responses and 3 in responses:
                    break
            if 2 not in responses or 3 not in responses:
                raise SchedulerError(
                    "PROBE_TIMEOUT",
                    "The App Server account and rate-limit reads did not complete in time",
                    retryable=True,
                )
            if responses[2].get("error") is not None or responses[3].get("error") is not None:
                raise SchedulerError(
                    "PROBE_REJECTED",
                    "The App Server rejected an account or rate-limit read",
                    retryable=False,
                )
            account = normalize_account_result(
                responses[2].get("result"),
                fingerprint_key=account_fingerprint_key,
            )
            return normalize_rate_limit_result(
                responses[3].get("result"),
                observed_at=self._clock(),
                profile_key=profile_key,
                limit_id=limit_id,
                account=account,
            )
        finally:
            try:
                process.stdin.close()
            except (OSError, AttributeError):
                pass
            self._terminate(process)
            try:
                process.stdout.close()
            except (OSError, AttributeError):
                pass

    @staticmethod
    def _terminate(process: Any) -> None:
        try:
            process.terminate()
            process.wait(timeout=1.0)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def _has_required_windows(bucket: Any) -> bool:
    if not isinstance(bucket, dict):
        return False
    durations = {
        window.get("windowDurationMins")
        for window in (bucket.get("primary"), bucket.get("secondary"))
        if isinstance(window, dict)
    }
    return {FIVE_HOUR_MINUTES, WEEKLY_MINUTES}.issubset(durations)


def normalize_account_result(value: Any, *, fingerprint_key: str) -> Dict[str, Any]:
    """Normalize account/read without returning or persisting the account email."""
    if not isinstance(value, dict):
        raise SchedulerError("PROBE_PROTOCOL_ERROR", "The App Server account response is missing")
    account = value.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise SchedulerError(
            "ACCOUNT_IDENTITY_UNAVAILABLE",
            "A ChatGPT account identity is required for quota binding",
        )
    email = account.get("email")
    if not isinstance(email, str) or not email.strip():
        raise SchedulerError(
            "ACCOUNT_IDENTITY_UNAVAILABLE",
            "The ChatGPT account has no stable identity field for local binding",
        )
    plan_type = account.get("planType")
    if plan_type is not None and not isinstance(plan_type, str):
        raise SchedulerError("SIGNAL_INVALID", "The account plan type is malformed")
    return {
        "account_fingerprint": keyed_fingerprint(
            {"email": email.strip().casefold(), "type": "chatgpt"}, fingerprint_key
        ),
        "account_plan_type": plan_type,
        "account_type": "chatgpt",
    }


def _select_bucket(result: Dict[str, Any], limit_id: str) -> Dict[str, Any]:
    by_id = result.get("rateLimitsByLimitId")
    if limit_id == "auto":
        matches = []
        if isinstance(by_id, dict):
            matches = [bucket for bucket in by_id.values() if _has_required_windows(bucket)]
        single = result.get("rateLimits")
        if not matches and _has_required_windows(single):
            matches = [single]
        if not matches:
            raise SchedulerError("SIGNAL_MISSING", "No Codex bucket contains both required windows")
        if len(matches) > 1:
            raise SchedulerError("SIGNAL_AMBIGUOUS", "Multiple Codex buckets contain both required windows")
        bucket = matches[0]
        if not isinstance(bucket.get("limitId"), str) or not bucket["limitId"]:
            raise SchedulerError("SIGNAL_INVALID", "The selected Codex bucket has no identifier")
        return bucket
    if isinstance(by_id, dict) and limit_id in by_id:
        bucket = by_id[limit_id]
    else:
        bucket = result.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") != limit_id:
        raise SchedulerError("SIGNAL_MISSING", "The configured Codex rate-limit bucket is missing")
    return bucket


def _paid_credit_state(bucket: Dict[str, Any]) -> str:
    """Return only what the supported credits snapshot proves.

    A missing or null credits object is not proof that paid fallback is off.
    Live dispatch therefore treats it as unknown and stops closed.
    """
    credits = bucket.get("credits")
    if credits is None:
        return "unknown"
    if not isinstance(credits, dict):
        raise SchedulerError("SIGNAL_INVALID", "The paid-credit signal is malformed")
    has_credits = credits.get("hasCredits")
    unlimited = credits.get("unlimited")
    if not isinstance(has_credits, bool) or not isinstance(unlimited, bool):
        raise SchedulerError("SIGNAL_INVALID", "The paid-credit signal is incomplete")
    if has_credits or unlimited:
        return "available"
    return "unavailable"


def _spend_control_state(result: Dict[str, Any], bucket: Dict[str, Any]) -> str:
    value = bucket.get("spendControlReached", result.get("spendControlReached"))
    if value is None:
        return "unknown"
    if not isinstance(value, bool):
        raise SchedulerError("SIGNAL_INVALID", "The spend-control signal is malformed")
    return "reached" if value else "not_reached"


def _normalize_window(value: Any, expected_minutes: int) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SchedulerError("SIGNAL_MISSING", "A required quota window is missing")
    duration = value.get("windowDurationMins")
    used = value.get("usedPercent")
    resets_at = value.get("resetsAt")
    if duration != expected_minutes:
        raise SchedulerError("SIGNAL_INVALID", "A quota window has an unexpected duration")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
        raise SchedulerError("SIGNAL_INVALID", "A quota window has an invalid usage percentage")
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)) or resets_at <= 0:
        raise SchedulerError("SIGNAL_INVALID", "A quota window has an invalid reset timestamp")
    return {
        "remaining_percent": 100.0 - float(used),
        "resets_at": float(resets_at),
        "used_percent": float(used),
        "window_duration_minutes": expected_minutes,
    }


def normalize_rate_limit_result(
    value: Any,
    *,
    observed_at: float,
    profile_key: str,
    limit_id: str,
    account: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SchedulerError("PROBE_PROTOCOL_ERROR", "The App Server response is missing a result")
    bucket = _select_bucket(value, limit_id)
    candidates = [bucket.get("primary"), bucket.get("secondary")]
    windows: Dict[int, Dict[str, Any]] = {}
    for candidate in candidates:
        if candidate is None:
            continue
        if not isinstance(candidate, dict):
            raise SchedulerError("SIGNAL_INVALID", "A quota window is malformed")
        duration = candidate.get("windowDurationMins")
        if duration in windows:
            raise SchedulerError("SIGNAL_INVALID", "A quota window duration is duplicated")
        if duration in {FIVE_HOUR_MINUTES, WEEKLY_MINUTES}:
            windows[duration] = candidate
    if FIVE_HOUR_MINUTES not in windows or WEEKLY_MINUTES not in windows:
        raise SchedulerError("SIGNAL_MISSING", "Both the 5-hour and weekly quota windows are required")
    plan_type = bucket.get("planType")
    if plan_type is not None and not isinstance(plan_type, str):
        plan_type = None
    reached = bucket.get("rateLimitReachedType")
    if reached is not None and not isinstance(reached, str):
        reached = "unknown"
    account_plan_type = account.get("account_plan_type")
    if plan_type is not None and account_plan_type is not None and plan_type != account_plan_type:
        raise SchedulerError("PROFILE_MISMATCH", "The account and quota plan types do not match")
    paid_credit_state = _paid_credit_state(bucket)
    spend_control_state = _spend_control_state(value, bucket)
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "observed_at": float(observed_at),
        "source": "codex_app_server",
        "profile_key": profile_key,
        "limit_id": bucket["limitId"],
        "account_fingerprint": account["account_fingerprint"],
        "account_type": account["account_type"],
        "plan_type": plan_type or account_plan_type,
        "credit_signal": {
            "available": "present",
            "unavailable": "absent",
            "unknown": "unknown",
        }[paid_credit_state],
        "paid_credit_state": paid_credit_state,
        "spend_control_state": spend_control_state,
        "rate_limit_reached_type": reached,
        "five_hour": _normalize_window(windows[FIVE_HOUR_MINUTES], FIVE_HOUR_MINUTES),
        "weekly": _normalize_window(windows[WEEKLY_MINUTES], WEEKLY_MINUTES),
    }
