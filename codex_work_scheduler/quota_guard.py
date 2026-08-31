"""Fail-closed quota guard domain and coordinator.

The guard keeps the durable part of a stop/resume protocol in SQLite while
all Codex task calls go through an injected adapter.  The coordinator never
holds a database transaction while it calls that adapter.  Adapter method
names are intentionally resolved at the boundary so the controller can bind a
concrete App Server adapter without changing the state machine.
"""

import inspect
import math
import time
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from .errors import SchedulerError
from .store import (
    QUOTA_GUARD_SESSION_STATES,
    QUOTA_GUARD_TARGET_STATES,
    Store,
)
from .util import new_id, payload_hash


QUOTA_GUARD_SESSION_STATES = frozenset(QUOTA_GUARD_SESSION_STATES)
QUOTA_GUARD_TARGET_STATES = frozenset(QUOTA_GUARD_TARGET_STATES)
WINDOW_NAMES = ("five_hour", "weekly")
DEFAULT_THRESHOLD_REMAINING_PERCENT = 10.0
DEFAULT_RESUME_HYSTERESIS_PERCENT = 5.0
DEFAULT_CHECK_INTERVAL_SECONDS = 30
DEFAULT_MAX_SNAPSHOT_AGE_SECONDS = 300


class QuotaGuardAdapter(Protocol):
    """The narrow task-control surface required by the guard.

    Implementations may use different concrete names.  The coordinator also
    accepts the equivalent aliases documented in ``_ADAPTER_METHODS`` below.
    """

    def inventory(self) -> Any:
        ...

    def read_thread(self, thread_id: str, include_turns: bool = True) -> Any:
        ...

    def get_goal(self, thread_id: str) -> Any:
        ...

    def set_goal_status(self, thread_id: str, status: str) -> Any:
        ...

    def interrupt(self, thread_id: str, turn_id: str) -> Any:
        ...

    def reopen_thread(self, thread_id: str) -> Any:
        ...

    def start_fixed_continuation(self, thread_id: str) -> Any:
        ...


Adapter = QuotaGuardAdapter

_ADAPTER_METHODS = {
    "inventory": ("inventory", "list_threads", "thread_inventory"),
    "read_thread": ("read_thread", "thread_read", "read"),
    "get_goal": ("get_goal", "read_goal"),
    "set_goal_status": ("set_goal_status", "update_goal_status"),
    "interrupt": ("interrupt", "interrupt_turn", "interrupt_thread"),
    "reopen": ("reopen_thread", "resume_thread", "reopen", "resume"),
    "continuation": (
        "start_fixed_continuation",
        "start_continuation",
        "start_continuation_turn",
        "start_turn",
    ),
}

_SIGNAL_REASON_CODES = {
    "SIGNAL_MISSING",
    "SIGNAL_STALE",
    "SIGNAL_INVALID",
    "SIGNAL_AMBIGUOUS",
    "PROFILE_MISMATCH",
    "ACCOUNT_IDENTITY_UNAVAILABLE",
}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return value


def _window_values(snapshot: Any) -> Optional[Dict[str, Dict[str, float]]]:
    if not isinstance(snapshot, dict):
        return None
    windows: Dict[str, Dict[str, float]] = {}
    for name in WINDOW_NAMES:
        value = snapshot.get(name)
        if not isinstance(value, dict):
            return None
        remaining = _number(value.get("remaining_percent"))
        used = _number(value.get("used_percent"))
        resets_at = _number(value.get("resets_at"))
        if remaining is None or used is None or resets_at is None:
            return None
        if not 0.0 <= remaining <= 100.0 or not 0.0 <= used <= 100.0:
            return None
        if resets_at <= 0.0:
            return None
        # Normalized snapshots carry both values.  Reject contradictory
        # values instead of choosing one and potentially resuming unsafely.
        if abs((100.0 - used) - remaining) > 0.000001:
            return None
        windows[name] = {
            "remaining_percent": remaining,
            "used_percent": used,
            "resets_at": resets_at,
        }
    return windows


def _signal_status(
    snapshot: Any,
    *,
    now: Optional[float],
    max_snapshot_age_seconds: float,
) -> Tuple[str, Optional[Dict[str, Dict[str, float]]], List[str]]:
    """Return ``(status, windows, reasons)`` without exposing values."""
    if snapshot is None:
        return "missing", None, ["SIGNAL_MISSING"]
    if not isinstance(snapshot, dict):
        return "invalid", None, ["SIGNAL_INVALID"]
    marker = snapshot.get("signal_status")
    markers = snapshot.get("reasons", ())
    if not isinstance(markers, (list, tuple, set)):
        markers = ()
    marker_reasons = {str(value) for value in markers if isinstance(value, str)}
    if marker in {"missing", "stale", "invalid", "ambiguous"}:
        code = "SIGNAL_%s" % str(marker).upper()
        return str(marker), None, [code]
    if snapshot.get("ambiguous") is True or "SIGNAL_AMBIGUOUS" in marker_reasons:
        return "ambiguous", None, ["SIGNAL_AMBIGUOUS"]
    if marker_reasons & _SIGNAL_REASON_CODES:
        code = sorted(marker_reasons & _SIGNAL_REASON_CODES)[0]
        return code.removeprefix("SIGNAL_").lower(), None, [code]
    windows = _window_values(snapshot)
    if windows is None:
        return "invalid", None, ["SIGNAL_INVALID"]
    observed_at = _number(snapshot.get("observed_at"))
    if observed_at is None:
        return "invalid", None, ["SIGNAL_INVALID"]
    compare_at = observed_at if now is None else float(now)
    age = compare_at - observed_at
    if age < -60.0:
        return "invalid", None, ["SIGNAL_INVALID"]
    if age > float(max_snapshot_age_seconds):
        return "stale", None, ["SIGNAL_STALE"]
    for window in windows.values():
        if window["resets_at"] <= compare_at:
            return "stale", None, ["SIGNAL_STALE"]
    return "fresh", windows, []


def _decision(
    *,
    should_trip_value: bool,
    contain: bool,
    signal_status: str,
    reasons: Sequence[str],
    tripped_windows: Sequence[str] = (),
) -> Dict[str, Any]:
    normalized = []
    for value in tripped_windows:
        if value in WINDOW_NAMES and value not in normalized:
            normalized.append(value)
    reason_values = []
    for value in reasons:
        if value not in reason_values:
            reason_values.append(value)
    return {
        "should_trip": bool(should_trip_value),
        "trip": bool(should_trip_value),
        "contain": bool(contain),
        "signal_status": signal_status,
        "reasons": reason_values,
        "reason_code": reason_values[0] if reason_values else None,
        "tripped_windows": normalized,
    }


def decide_trip(
    snapshot: Optional[Dict[str, Any]],
    threshold_remaining_percent: float,
    *,
    now: Optional[float] = None,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> Dict[str, Any]:
    """Decide whether a fresh quota snapshot requires containment.

    Missing, stale, malformed, or ambiguous signals return ``contain=True``.
    A held session can remain held for those decisions; an armed session must
    move toward containment.
    """
    threshold = _number(threshold_remaining_percent)
    if threshold is None or threshold < 0.0 or threshold > 100.0:
        return _decision(
            should_trip_value=True,
            contain=True,
            signal_status="invalid",
            reasons=("SIGNAL_INVALID",),
        )
    status, windows, reasons = _signal_status(
        snapshot,
        now=now,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
    )
    if status != "fresh" or windows is None:
        return _decision(
            should_trip_value=True,
            contain=True,
            signal_status=status,
            reasons=reasons or ("SIGNAL_INVALID",),
        )
    tripped = [name for name in WINDOW_NAMES if windows[name]["remaining_percent"] <= threshold]
    return _decision(
        should_trip_value=bool(tripped),
        contain=bool(tripped),
        signal_status="fresh",
        reasons=("QUOTA_THRESHOLD",) if tripped else (),
        tripped_windows=tripped,
    )


trip_decision = decide_trip


def should_trip(
    snapshot: Optional[Dict[str, Any]],
    threshold_remaining_percent: float,
    *,
    now: Optional[float] = None,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> bool:
    return bool(
        decide_trip(
            snapshot,
            threshold_remaining_percent,
            now=now,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
        )["should_trip"]
    )


def _stop_trip_windows(
    stop_snapshot: Dict[str, Any],
    threshold_remaining_percent: float,
    explicit: Optional[Iterable[str]],
) -> List[str]:
    if explicit is not None:
        return [name for name in WINDOW_NAMES if name in set(explicit)]
    stored = stop_snapshot.get("tripped_windows")
    if isinstance(stored, (list, tuple, set)):
        selected = [name for name in WINDOW_NAMES if name in set(stored)]
        if selected:
            return selected
    windows = _window_values(stop_snapshot)
    threshold = _number(threshold_remaining_percent)
    if windows is None or threshold is None:
        return []
    return [name for name in WINDOW_NAMES if windows[name]["remaining_percent"] <= threshold]


def decide_resume(
    snapshot: Optional[Dict[str, Any]],
    stop_snapshot: Optional[Dict[str, Any]],
    threshold_remaining_percent: float,
    resume_hysteresis_percent: float,
    *,
    tripped_windows: Optional[Iterable[str]] = None,
    now: Optional[float] = None,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> Dict[str, Any]:
    """Decide whether quota reset and hysteresis are both proven.

    Reset proof is strict: for every window that caused the stop, the reset
    timestamp must advance and usage must fall relative to the stop snapshot.
    """
    threshold = _number(threshold_remaining_percent)
    hysteresis = _number(resume_hysteresis_percent)
    if (
        threshold is None
        or hysteresis is None
        or threshold < 0.0
        or hysteresis < 0.0
        or threshold > 100.0
        or hysteresis > 100.0
    ):
        return {
            "safe_to_resume": False,
            "can_resume": False,
            "signal_status": "invalid",
            "reasons": ["SIGNAL_INVALID"],
            "reset_confirmed": False,
            "hysteresis_met": False,
            "tripped_windows": [],
        }
    status, windows, reasons = _signal_status(
        snapshot,
        now=now,
        max_snapshot_age_seconds=max_snapshot_age_seconds,
    )
    if status != "fresh" or windows is None:
        return {
            "safe_to_resume": False,
            "can_resume": False,
            "signal_status": status,
            "reasons": reasons or ["SIGNAL_INVALID"],
            "reset_confirmed": False,
            "hysteresis_met": False,
            "tripped_windows": [],
        }
    stop_windows = _window_values(stop_snapshot)
    caused = _stop_trip_windows(
        stop_snapshot if isinstance(stop_snapshot, dict) else {},
        threshold,
        tripped_windows,
    )
    if stop_windows is None or not caused:
        return {
            "safe_to_resume": False,
            "can_resume": False,
            "signal_status": "fresh",
            "reasons": ["RESET_NOT_CONFIRMED"],
            "reset_confirmed": False,
            "hysteresis_met": False,
            "tripped_windows": caused,
        }
    hysteresis_limit = threshold + hysteresis
    hysteresis_met = all(
        windows[name]["remaining_percent"] > hysteresis_limit for name in WINDOW_NAMES
    )
    reset_confirmed = all(
        windows[name]["resets_at"] > stop_windows[name]["resets_at"]
        and windows[name]["used_percent"] < stop_windows[name]["used_percent"]
        for name in caused
    )
    reasons_out: List[str] = []
    if not hysteresis_met:
        reasons_out.append("HYSTERESIS_NOT_MET")
    if not reset_confirmed:
        reasons_out.append("RESET_NOT_CONFIRMED")
    return {
        "safe_to_resume": bool(hysteresis_met and reset_confirmed),
        "can_resume": bool(hysteresis_met and reset_confirmed),
        "signal_status": "fresh",
        "reasons": reasons_out,
        "reset_confirmed": bool(reset_confirmed),
        "hysteresis_met": bool(hysteresis_met),
        "tripped_windows": caused,
    }


resume_decision = decide_resume


def can_resume(
    snapshot: Optional[Dict[str, Any]],
    stop_snapshot: Optional[Dict[str, Any]],
    threshold_remaining_percent: float,
    resume_hysteresis_percent: float,
    *,
    tripped_windows: Optional[Iterable[str]] = None,
    now: Optional[float] = None,
    max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
) -> bool:
    return bool(
        decide_resume(
            snapshot,
            stop_snapshot,
            threshold_remaining_percent,
            resume_hysteresis_percent,
            tripped_windows=tripped_windows,
            now=now,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
        )["safe_to_resume"]
    )


class QuotaGuardCoordinator:
    """Durable, sequential quota stop/resume coordinator."""

    def __init__(
        self,
        store: Store,
        adapter: QuotaGuardAdapter,
        *,
        clock: Any = time.time,
        max_snapshot_age_seconds: float = DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
        actor: str = "quota_guard",
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.clock = clock
        self.max_snapshot_age_seconds = float(max_snapshot_age_seconds)
        self.actor = actor

    # ------------------------------ plan and redaction -----------------

    @staticmethod
    def _plan_settings(plan: Dict[str, Any]) -> Dict[str, Any]:
        nested = plan.get("quota_guard")
        if not isinstance(nested, dict):
            nested = plan
        resume = nested.get("resume")
        if not isinstance(resume, dict):
            resume = {}
        return {
            "profile_key": nested.get("profile_key", plan.get("profile_key", "default")),
            "limit_id": nested.get("limit_id", plan.get("limit_id", "auto")),
            "threshold_remaining_percent": nested.get(
                "threshold_remaining_percent",
                nested.get("threshold_percent", nested.get("threshold", DEFAULT_THRESHOLD_REMAINING_PERCENT)),
            ),
            "resume_hysteresis_percent": nested.get(
                "resume_hysteresis_percent",
                nested.get("hysteresis_percent", nested.get("hysteresis", DEFAULT_RESUME_HYSTERESIS_PERCENT)),
            ),
            "check_interval_seconds": nested.get(
                "check_interval_seconds",
                nested.get("interval_seconds", DEFAULT_CHECK_INTERVAL_SECONDS),
            ),
            "resume_non_goal_threads": nested.get(
                "resume_non_goal_threads",
                resume.get("non_goal_threads", resume.get("resume_non_goal_threads", False)),
            ),
        }

    @staticmethod
    def _plan_targets(plan: Dict[str, Any]) -> List[Any]:
        raw = plan.get(
            "target_thread_ids",
            plan.get("targets", plan.get("thread_ids", plan.get("threads", ()))),
        )
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, (list, tuple, set)):
            raise SchedulerError("STATE_INVALID", "The quota-guard plan targets are invalid")
        result: List[Any] = []
        seen = set()
        for value in raw:
            candidate = value if isinstance(value, str) else value.get("thread_id") if isinstance(value, dict) else None
            if not isinstance(candidate, str) or not candidate:
                raise SchedulerError("STATE_INVALID", "The quota-guard plan target is invalid")
            if candidate in seen:
                raise SchedulerError("STATE_INVALID", "The quota-guard plan targets are not unique")
            seen.add(candidate)
            result.append(value)
        return result

    @staticmethod
    def _redacted_target(target: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "thread_id": target["thread_id"],
            "state": target["state"],
            "original_status": target["original_status"],
            "original_turn_id": target["original_turn_id"],
            "goal_was_active": bool(target["goal_was_active"]),
            "goal_changed_by_guard": bool(target["goal_changed_by_guard"]),
            "reason_code": target["reason_code"],
            "revision": target["revision"],
        }

    def _redacted_session(self, session: Dict[str, Any]) -> Dict[str, Any]:
        targets = self.store.list_quota_guard_targets(session["guard_id"])
        return {
            "guard_id": session["guard_id"],
            "state": session["state"],
            "profile_key": session["profile_key"],
            "limit_id": session["limit_id"],
            "threshold_remaining_percent": session["threshold_remaining_percent"],
            "resume_hysteresis_percent": session["resume_hysteresis_percent"],
            "check_interval_seconds": session["check_interval_seconds"],
            "resume_non_goal_threads": bool(session["resume_non_goal_threads"]),
            "approval_id": session["approval_id"],
            "plan_hash": session["plan_hash"],
            "stop_snapshot_hash": session["stop_snapshot_hash"],
            "tripped_windows": list(session["tripped_windows"]),
            "next_check_at": session["next_check_at"],
            "last_checked_at": session["last_checked_at"],
            "reason_code": session["reason_code"],
            "revision": session["revision"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "target_count": len(targets),
            "targets": [self._redacted_target(target) for target in targets],
        }

    def _audit(self, event_type: str, *, guard_id: str, details: Dict[str, Any], now: float) -> None:
        # Details are deliberately limited to state, opaque identifiers, and
        # window names.  No quota values or task content are ever audited.
        safe = {"guard_id": guard_id}
        for key in ("state", "reason_code", "tripped_windows", "target_state", "thread_id"):
            if key in details:
                value = details[key]
                if key == "tripped_windows":
                    safe[key] = [name for name in value if name in WINDOW_NAMES]
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    safe[key] = value
        with self.store.transaction() as connection:
            self.store.append_audit(
                connection,
                event_type=event_type,
                actor=self.actor,
                details=safe,
                now=now,
            )

    def create(
        self,
        plan: Dict[str, Any],
        approval_id: str,
        *,
        guard_id: Optional[str] = None,
        now: Optional[float] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(plan, dict):
            raise SchedulerError("STATE_INVALID", "The quota-guard plan is invalid")
        settings = self._plan_settings(plan)
        targets = self._plan_targets(plan)
        selected_id = guard_id or plan.get("guard_id") or new_id("guard")
        if not isinstance(selected_id, str) or not selected_id:
            raise SchedulerError("STATE_INVALID", "The quota-guard id is invalid")
        if not isinstance(approval_id, str) or not approval_id:
            raise SchedulerError("STATE_INVALID", "The quota-guard approval is invalid")
        observed_at = float(self.clock() if now is None else now)
        plan_digest = payload_hash(plan)
        try:
            existing = self.store.get_quota_guard_session(selected_id)
        except SchedulerError as exc:
            if exc.code != "GUARD_NOT_FOUND":
                raise
            existing = None
        if existing is not None:
            if existing["plan_hash"] == plan_digest and existing["approval_id"] == approval_id:
                return self._redacted_session(existing)
            raise SchedulerError("GUARD_EXISTS", "The quota-guard id is already bound to another plan")
        session = self.store.create_quota_guard_session(
            guard_id=selected_id,
            state="ARMED",
            profile_key=str(settings["profile_key"]),
            limit_id=str(settings["limit_id"]),
            threshold_remaining_percent=float(settings["threshold_remaining_percent"]),
            resume_hysteresis_percent=float(settings["resume_hysteresis_percent"]),
            check_interval_seconds=int(settings["check_interval_seconds"]),
            resume_non_goal_threads=bool(settings["resume_non_goal_threads"]),
            approval_id=approval_id,
            plan_hash=plan_digest,
            tripped_windows=(),
            next_check_at=observed_at,
            last_checked_at=None,
            reason_code=None,
            revision=1,
            created_at=observed_at,
            updated_at=observed_at,
            targets=targets,
        )
        self._audit(
            "quota_guard.created",
            guard_id=selected_id,
            details={"state": session["state"]},
            now=observed_at,
        )
        return self._redacted_session(session)

    create_and_arm = create

    def arm(
        self,
        guard_or_plan: Any,
        approval_id: Optional[str] = None,
        *,
        guard_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        if isinstance(guard_or_plan, dict):
            if approval_id is None:
                raise SchedulerError("STATE_INVALID", "The quota-guard approval is required")
            return self.create(guard_or_plan, approval_id, guard_id=guard_id, now=now)
        selected_id = guard_id or guard_or_plan
        session = self.store.get_quota_guard_session(selected_id)
        if session["state"] == "DISARMED":
            raise SchedulerError("GUARD_DISARMED", "The quota guard is disarmed")
        if session["state"] == "NEEDS_REVIEW":
            raise SchedulerError("GUARD_NEEDS_REVIEW", "The quota guard needs review")
        if session["state"] == "ARMED":
            return self._redacted_session(session)
        updated = self.store.transition_quota_guard_session(
            selected_id,
            "ARMED",
            expected_revision=session["revision"],
            expected_state=session["state"],
            now=float(self.clock() if now is None else now),
            reason_code=None,
        )
        return self._redacted_session(updated)

    def status(self, guard_id: str) -> Dict[str, Any]:
        return self._redacted_session(self.store.get_quota_guard_session(guard_id))

    get_status = status

    def list_sessions(self) -> List[Dict[str, Any]]:
        return [self._redacted_session(row) for row in self.store.list_quota_guard_sessions()]

    list_guards = list_sessions

    def disarm(self, guard_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
        observed_at = float(self.clock() if now is None else now)
        session = self.store.get_quota_guard_session(guard_id)
        if session["state"] == "DISARMED":
            return self._redacted_session(session)
        updated = self.store.transition_quota_guard_session(
            guard_id,
            "DISARMED",
            expected_revision=session["revision"],
            expected_state=session["state"],
            now=observed_at,
            reason_code="OPERATOR_DISARM",
        )
        self._audit(
            "quota_guard.disarmed",
            guard_id=guard_id,
            details={"state": updated["state"], "reason_code": updated["reason_code"]},
            now=observed_at,
        )
        return self._redacted_session(updated)

    # ------------------------------ adapter boundary -------------------

    def _adapter_method(self, operation: str, *, required: bool = True) -> Optional[Any]:
        for name in _ADAPTER_METHODS[operation]:
            method = getattr(self.adapter, name, None)
            if callable(method):
                return method
        if required:
            raise SchedulerError("ADAPTER_UNAVAILABLE", "The quota-guard adapter operation is unavailable")
        return None

    @staticmethod
    def _invoke(method: Any, args: Sequence[Any] = (), kwargs: Optional[Dict[str, Any]] = None) -> Any:
        kwargs = dict(kwargs or {})
        if kwargs:
            try:
                signature = inspect.signature(method)
                parameters = signature.parameters.values()
                accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
                if not accepts_kwargs:
                    allowed = set(signature.parameters)
                    kwargs = {key: value for key, value in kwargs.items() if key in allowed}
            except (TypeError, ValueError):
                pass
        return method(*args, **kwargs)

    def _call(self, operation: str, *args: Any, required: bool = True, **kwargs: Any) -> Any:
        method = self._adapter_method(operation, required=required)
        if method is None:
            return None
        return self._invoke(method, args=args, kwargs=kwargs)

    def _inventory(self) -> Optional[set]:
        value = self._call("inventory", required=False)
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get("threads", value.get("items", value.get("inventory", value)))
        if not isinstance(value, (list, tuple, set)):
            raise SchedulerError("INVENTORY_INVALID", "The task inventory is malformed")
        result = set()
        for item in value:
            if isinstance(item, str):
                result.add(item)
            elif isinstance(item, dict):
                candidate = item.get("thread_id", item.get("id"))
                if isinstance(candidate, str):
                    result.add(candidate)
        return result

    def _read_thread(self, thread_id: str) -> Dict[str, Any]:
        value = self._call("read_thread", thread_id, include_turns=True)
        if not isinstance(value, dict):
            raise SchedulerError("THREAD_READ_INVALID", "The task read is malformed")
        thread = value.get("thread") if isinstance(value.get("thread"), dict) else value
        if not isinstance(thread, dict):
            raise SchedulerError("THREAD_READ_INVALID", "The task read is malformed")
        returned_id = thread.get("id", thread.get("thread_id"))
        if returned_id is not None and returned_id != thread_id:
            raise SchedulerError("THREAD_ID_MISMATCH", "The task read identity does not match")
        status = thread.get("status", thread.get("thread_status"))
        if isinstance(status, dict):
            status = status.get("type", status.get("status"))
        if status not in {"active", "idle", "notLoaded", "systemError"}:
            raise SchedulerError("THREAD_READ_INVALID", "The task status is unknown")
        active_turn_id = thread.get(
            "active_turn_id", value.get("active_turn_id")
        )
        if active_turn_id is not None and (
            not isinstance(active_turn_id, str) or not active_turn_id
        ):
            raise SchedulerError("THREAD_READ_INVALID", "The active turn identity is malformed")
        turns = thread.get("turns", value.get("turns", []))
        if turns is None:
            turns = []
        if not isinstance(turns, list):
            raise SchedulerError("THREAD_READ_INVALID", "The task turns are malformed")
        normalized_turns: List[Dict[str, Any]] = []
        seen_turns = set()
        for turn in turns:
            if not isinstance(turn, dict):
                raise SchedulerError("THREAD_READ_INVALID", "The task turn is malformed")
            turn_id = turn.get("id", turn.get("turn_id"))
            turn_status = turn.get("status", turn.get("turn_status"))
            if turn_id is not None and not isinstance(turn_id, str):
                raise SchedulerError("THREAD_READ_INVALID", "The task turn identity is malformed")
            if turn_id in seen_turns:
                raise SchedulerError("THREAD_READ_INVALID", "The task contains duplicate turn identities")
            seen_turns.add(turn_id)
            if turn_status not in {"completed", "interrupted", "failed", "inProgress"}:
                raise SchedulerError("THREAD_READ_INVALID", "The task turn status is unknown")
            normalized_turns.append({"id": turn_id, "status": turn_status})
        active_turns = [
            turn["id"] for turn in normalized_turns if turn["status"] == "inProgress"
        ]
        if len(active_turns) > 1:
            raise SchedulerError("THREAD_READ_INVALID", "The task has multiple active turns")
        if active_turn_id is not None and active_turns and active_turn_id != active_turns[0]:
            raise SchedulerError("THREAD_READ_INVALID", "The active turn identities conflict")
        if status != "active" and (active_turn_id is not None or active_turns):
            raise SchedulerError("THREAD_READ_INVALID", "The task status conflicts with its active turn")
        return {
            "thread_id": thread_id,
            "status": status,
            "active_turn_id": active_turn_id or (active_turns[0] if active_turns else None),
            "turns": normalized_turns,
        }

    @staticmethod
    def _thread_completed(read: Dict[str, Any]) -> bool:
        status = str(read.get("status") or "").casefold()
        if status in {"completed", "succeeded", "done", "closed", "terminated"}:
            return True
        turns = read.get("turns", [])
        if status in {"idle", "notloaded", "not_loaded"} and turns:
            latest = turns[-1].get("status")
            # An interrupted turn can be the guard's own stop and is
            # resumable.  Treat only terminal non-interrupted turns as
            # completed here.
            return str(latest or "").casefold() in {"completed", "succeeded", "failed", "done"}
        return False

    @staticmethod
    def _active_turn(read: Dict[str, Any]) -> Optional[str]:
        explicit = read.get("active_turn_id")
        if isinstance(explicit, str) and explicit:
            return explicit
        active_statuses = {"inprogress", "in_progress", "active", "running", "started"}
        for turn in reversed(read.get("turns", [])):
            turn_id = turn.get("id")
            status = str(turn.get("status") or "").casefold()
            if isinstance(turn_id, str) and status in active_statuses:
                return turn_id
        return None

    @staticmethod
    def _turn_terminal(read: Dict[str, Any], turn_id: str) -> Optional[str]:
        for turn in read.get("turns", []):
            if turn.get("id") == turn_id:
                status = str(turn.get("status") or "").casefold()
                if status in {"interrupted", "completed", "succeeded", "failed", "done", "terminated"}:
                    return status
                return None
        return None

    @staticmethod
    def _goal(value: Any) -> Tuple[bool, str, Optional[int]]:
        if value is None:
            return False, "none", None
        if isinstance(value, dict):
            goal = value.get("goal") if isinstance(value.get("goal"), dict) else value
            status = goal.get("status", goal.get("state"))
            if status is None and "active" in goal and isinstance(goal["active"], bool):
                status = "active" if goal["active"] else "paused"
            if status is None:
                return False, "none", None
            updated_at = goal.get("updated_at", goal.get("updatedAt"))
            if updated_at is not None and (
                isinstance(updated_at, bool) or not isinstance(updated_at, int) or updated_at < 0
            ):
                raise SchedulerError("GOAL_READ_INVALID", "The task goal timestamp is malformed")
            normalized = str(status).casefold()
            if normalized in {"active", "running", "inprogress", "in_progress"}:
                return True, "active", updated_at
            if normalized in {"paused", "pause", "held", "suspended"}:
                return True, "paused", updated_at
            if normalized in {"completed", "done", "succeeded", "failed"}:
                return True, normalized, updated_at
            return True, normalized, updated_at
        if isinstance(value, str):
            normalized = value.casefold()
            return normalized in {"active", "running", "inprogress", "in_progress"}, normalized, None
        if isinstance(value, bool):
            return True, "active" if value else "paused", None
        raise SchedulerError("GOAL_READ_INVALID", "The task goal state is malformed")

    @staticmethod
    def _reason(exc: Exception, fallback: str = "ADAPTER_UNCERTAIN") -> str:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return code[:64]
        return fallback

    def _controller_allows_resume(self) -> bool:
        try:
            return self.store.controller()["mode"] not in {"BLOCKED", "STOPPED"}
        except SchedulerError as exc:
            # The pure coordinator is also used against a domain-only Store in
            # tests and embedded callers. Controller bootstrapping is enforced
            # by the service path before any operational cycle.
            if exc.code == "STATE_INVALID":
                return True
            raise

    # ------------------------------ local state helpers -----------------

    def _mark_target_review(self, target: Dict[str, Any], reason: str, now: float) -> Dict[str, Any]:
        if target["state"] == "NEEDS_REVIEW":
            return target
        try:
            return self.store.transition_quota_guard_target(
                target["guard_id"],
                target["thread_id"],
                "NEEDS_REVIEW",
                expected_revision=target["revision"],
                expected_state=target["state"],
                now=now,
                reason_code=reason,
            )
        except SchedulerError:
            return self.store.get_quota_guard_target(target["guard_id"], target["thread_id"])

    def _mark_session_review(self, session: Dict[str, Any], reason: str, now: float) -> Dict[str, Any]:
        if session["state"] in {"NEEDS_REVIEW", "DISARMED"}:
            return session
        try:
            return self.store.transition_quota_guard_session(
                session["guard_id"],
                "NEEDS_REVIEW",
                expected_revision=session["revision"],
                expected_state=session["state"],
                now=now,
                reason_code=reason,
            )
        except SchedulerError:
            return self.store.get_quota_guard_session(session["guard_id"])

    def _schedule_check(
        self,
        session: Dict[str, Any],
        *,
        state: Optional[str] = None,
        reason_code: Optional[str] = None,
        now: float,
    ) -> Dict[str, Any]:
        if session["state"] == "DISARMED":
            return session
        updates: Dict[str, Any] = {
            "next_check_at": now + float(session["check_interval_seconds"]),
            "last_checked_at": now,
            "reason_code": reason_code,
        }
        if state is None or state == session["state"]:
            return self.store.update_quota_guard_session(
                session["guard_id"],
                expected_revision=session["revision"],
                expected_state=session["state"],
                now=now,
                **updates,
            )
        return self.store.transition_quota_guard_session(
            session["guard_id"],
            state,
            expected_revision=session["revision"],
            expected_state=session["state"],
            now=now,
            **updates,
        )

    # ------------------------------ containment -------------------------

    def _contain_target(self, target: Dict[str, Any], now: float) -> Dict[str, Any]:
        if target["state"] in {"COMPLETED", "NEEDS_REVIEW"}:
            return target
        thread_id = target["thread_id"]
        read = self._read_thread(thread_id)
        if self._thread_completed(read):
            return self.store.transition_quota_guard_target(
                target["guard_id"],
                thread_id,
                "COMPLETED",
                expected_revision=target["revision"],
                expected_state=target["state"],
                now=now,
                original_status=read.get("status"),
            )
        goal_value = self._call("get_goal", thread_id)
        goal_exists, goal_state, goal_updated_at = self._goal(goal_value)
        goal_active = goal_exists and goal_state == "active"
        if target["goal_changed_by_guard"]:
            if (
                not goal_exists
                or goal_state != "paused"
                or goal_updated_at is None
                or goal_updated_at != target["goal_pause_updated_at"]
            ):
                raise SchedulerError(
                    "GOAL_OWNERSHIP_UNCONFIRMED",
                    "The guard-owned goal pause changed while held",
                )
        elif target["original_status"] is not None and goal_active and not target["goal_was_active"]:
            raise SchedulerError(
                "GOAL_OWNERSHIP_UNCONFIRMED",
                "A goal appeared after the guard began monitoring the target",
            )
        original_changes: Dict[str, Any] = {}
        if target["original_status"] is None:
            original_changes["original_status"] = read.get("status")
            original_changes["goal_was_active"] = int(goal_active)
        if original_changes:
            target = self.store.update_quota_guard_target(
                target["guard_id"],
                thread_id,
                expected_revision=target["revision"],
                expected_state=target["state"],
                now=now,
                **original_changes,
            )
        if goal_active:
            if goal_updated_at is None:
                raise SchedulerError(
                    "GOAL_OWNERSHIP_UNCONFIRMED",
                    "The active goal has no ownership timestamp",
                )
            if self.store.get_quota_guard_session(target["guard_id"])["state"] == "DISARMED":
                return target
            self._call("set_goal_status", thread_id, "paused")
            verify_value = self._call("get_goal", thread_id)
            verify_exists, verify_state, verify_updated_at = self._goal(verify_value)
            if (
                not verify_exists
                or verify_state != "paused"
                or verify_updated_at is None
                or verify_updated_at <= goal_updated_at
            ):
                raise SchedulerError("GOAL_PAUSE_UNCONFIRMED", "The task goal did not become paused")
            target = self.store.update_quota_guard_target(
                target["guard_id"],
                thread_id,
                expected_revision=target["revision"],
                expected_state=target["state"],
                now=now,
                goal_changed_by_guard=1,
                goal_pause_updated_at=verify_updated_at,
            )
        # Read again after goal pause.  This pins the exact turn that is
        # interrupted and prevents a stale turn id from being used.
        after_goal = self._read_thread(thread_id)
        if self._thread_completed(after_goal):
            return self.store.transition_quota_guard_target(
                target["guard_id"],
                thread_id,
                "COMPLETED",
                expected_revision=target["revision"],
                expected_state=target["state"],
                now=now,
                original_status=target["original_status"] or after_goal.get("status"),
            )
        turn_id = self._active_turn(after_goal)
        if turn_id is None:
            if after_goal.get("status") == "active":
                raise SchedulerError(
                    "TURN_MISSING",
                    "The task reports active work without an exact active turn",
                )
            return self.store.transition_quota_guard_target(
                target["guard_id"],
                thread_id,
                "HELD",
                expected_revision=target["revision"],
                expected_state=target["state"],
                now=now,
            )
        stopping = self.store.transition_quota_guard_target(
            target["guard_id"],
            thread_id,
            "STOPPING",
            expected_revision=target["revision"],
            expected_state=target["state"],
            now=now,
            original_turn_id=turn_id,
        )
        try:
            if self.store.get_quota_guard_session(target["guard_id"])["state"] == "DISARMED":
                return stopping
            self._call("interrupt", thread_id, turn_id)
            verified = self._read_thread(thread_id)
            terminal = self._turn_terminal(verified, turn_id)
            if terminal is None:
                raise SchedulerError("TURN_INTERRUPT_UNCONFIRMED", "The exact turn did not confirm interruption")
            if self._active_turn(verified) is not None or verified.get("status") == "active":
                raise SchedulerError(
                    "REPLACEMENT_TURN_ACTIVE",
                    "Another active turn appeared after the interrupt",
                )
            if terminal in {"completed", "succeeded", "failed", "done", "terminated"}:
                final_state = "COMPLETED"
            else:
                final_state = "HELD"
            return self.store.transition_quota_guard_target(
                stopping["guard_id"],
                thread_id,
                final_state,
                expected_revision=stopping["revision"],
                expected_state="STOPPING",
                now=now,
                reason_code=None,
            )
        except Exception as exc:
            self._mark_target_review(stopping, self._reason(exc), now)
            raise

    def _contain_all(self, session: Dict[str, Any], now: float, reason: str) -> Dict[str, Any]:
        for target in self.store.list_quota_guard_targets(session["guard_id"]):
            if target["state"] in {"COMPLETED", "NEEDS_REVIEW"}:
                continue
            try:
                self._contain_target(target, now)
            except Exception as exc:
                # _contain_target fences a committed STOPPING target itself;
                # fence any earlier uncertainty here as well.
                current = self.store.get_quota_guard_target(session["guard_id"], target["thread_id"])
                if current["state"] not in {"COMPLETED", "HELD", "NEEDS_REVIEW"}:
                    self._mark_target_review(current, self._reason(exc), now)
        current_targets = self.store.list_quota_guard_targets(session["guard_id"])
        unresolved = any(target["state"] == "NEEDS_REVIEW" for target in current_targets)
        if unresolved:
            return self._mark_session_review(session, reason or "CONTAINMENT_UNCERTAIN", now)
        try:
            fresh = self.store.get_quota_guard_session(session["guard_id"])
            return self._schedule_check(
                fresh,
                state="HELD_QUOTA",
                reason_code=reason or "QUOTA_THRESHOLD",
                now=now,
            )
        except SchedulerError:
            return self.store.get_quota_guard_session(session["guard_id"])

    # ------------------------------ resumption -------------------------

    def _preflight_held_targets(self, session: Dict[str, Any], now: float) -> bool:
        for target in self.store.list_quota_guard_targets(session["guard_id"]):
            if target["state"] in {"COMPLETED", "NEEDS_REVIEW", "RESUMED"}:
                continue
            try:
                self._contain_target(target, now)
            except Exception as exc:
                current = self.store.get_quota_guard_target(session["guard_id"], target["thread_id"])
                if current["state"] not in {"COMPLETED", "HELD", "NEEDS_REVIEW"}:
                    self._mark_target_review(current, self._reason(exc), now)
        return not any(
            target["state"] == "NEEDS_REVIEW"
            for target in self.store.list_quota_guard_targets(session["guard_id"])
        )

    def _resume_target(self, target: Dict[str, Any], session: Dict[str, Any], now: float) -> Dict[str, Any]:
        if target["state"] in {"COMPLETED", "RESUMED", "NEEDS_REVIEW"}:
            return target
        read = self._read_thread(target["thread_id"])
        if self._thread_completed(read):
            return self.store.transition_quota_guard_target(
                target["guard_id"],
                target["thread_id"],
                "COMPLETED",
                expected_revision=target["revision"],
                expected_state=target["state"],
                now=now,
            )
        if self._active_turn(read) is not None or read.get("status") == "active":
            raise SchedulerError(
                "PREEXISTING_TURN_ACTIVE",
                "A task became active before the guard could resume it",
            )
        resuming = self.store.transition_quota_guard_target(
            target["guard_id"],
            target["thread_id"],
            "RESUMING",
            expected_revision=target["revision"],
            expected_state=target["state"],
            now=now,
        )
        try:
            thread_id = target["thread_id"]
            if target["goal_changed_by_guard"]:
                goal_value = self._call("get_goal", thread_id)
                goal_exists, goal_state, goal_updated_at = self._goal(goal_value)
                if (
                    not goal_exists
                    or goal_state != "paused"
                    or goal_updated_at is None
                    or goal_updated_at != target["goal_pause_updated_at"]
                ):
                    raise SchedulerError("GOAL_OWNERSHIP_UNCONFIRMED", "The goal is not paused by the guard")
                resume_read = self._read_thread(thread_id)
                if self._active_turn(resume_read) is not None or resume_read.get("status") == "active":
                    raise SchedulerError(
                        "PREEXISTING_TURN_ACTIVE",
                        "A task became active before goal restoration",
                    )
                if self.store.get_quota_guard_session(target["guard_id"])["state"] == "DISARMED":
                    return resuming
                if not self._controller_allows_resume():
                    raise SchedulerError(
                        "CONTROLLER_RESUME_DISABLED",
                        "The controller mode changed before goal resume",
                    )
                self._call("set_goal_status", thread_id, "active")
                verify_value = self._call("get_goal", thread_id)
                verify_exists, verify_state, verify_updated_at = self._goal(verify_value)
                if (
                    not verify_exists
                    or verify_state != "active"
                    or verify_updated_at is None
                    or verify_updated_at <= goal_updated_at
                ):
                    raise SchedulerError("GOAL_RESUME_UNCONFIRMED", "The goal did not become active")
            elif session["resume_non_goal_threads"]:
                resume_read = self._read_thread(thread_id)
                if self._active_turn(resume_read) is not None or resume_read.get("status") == "active":
                    raise SchedulerError(
                        "PREEXISTING_TURN_ACTIVE",
                        "A task became active before continuation",
                    )
                if self.store.get_quota_guard_session(target["guard_id"])["state"] == "DISARMED":
                    return resuming
                if not self._controller_allows_resume():
                    raise SchedulerError(
                        "CONTROLLER_RESUME_DISABLED",
                        "The controller mode changed before task reopen",
                    )
                self._call("reopen", thread_id)
                if self.store.get_quota_guard_session(target["guard_id"])["state"] == "DISARMED":
                    return resuming
                if not self._controller_allows_resume():
                    raise SchedulerError(
                        "CONTROLLER_RESUME_DISABLED",
                        "The controller mode changed before task continuation",
                    )
                continuation = self._call("continuation", thread_id)
                if not isinstance(continuation, dict):
                    raise SchedulerError(
                        "CONTINUATION_UNCONFIRMED",
                        "The fixed continuation response is malformed",
                    )
                continuation_turn_id = continuation.get(
                    "turn_id", continuation.get("turnId")
                )
                if not isinstance(continuation_turn_id, str) or not continuation_turn_id:
                    raise SchedulerError(
                        "CONTINUATION_UNCONFIRMED",
                        "The fixed continuation returned no turn identity",
                    )
                verified = self._read_thread(thread_id)
                if self._thread_completed(verified):
                    return self.store.transition_quota_guard_target(
                        resuming["guard_id"],
                        thread_id,
                        "COMPLETED",
                        expected_revision=resuming["revision"],
                        expected_state="RESUMING",
                        now=now,
                    )
                if self._active_turn(verified) != continuation_turn_id:
                    raise SchedulerError("CONTINUATION_UNCONFIRMED", "The fixed continuation did not become active")
            else:
                return self.store.transition_quota_guard_target(
                    resuming["guard_id"],
                    thread_id,
                    "HELD",
                    expected_revision=resuming["revision"],
                    expected_state="RESUMING",
                    now=now,
                    reason_code="RESUME_NOT_REQUESTED",
                )
            return self.store.transition_quota_guard_target(
                resuming["guard_id"],
                thread_id,
                "RESUMED",
                expected_revision=resuming["revision"],
                expected_state="RESUMING",
                now=now,
                reason_code=None,
            )
        except Exception as exc:
            self._mark_target_review(resuming, self._reason(exc), now)
            raise

    def _resume_all(self, session: Dict[str, Any], now: float) -> Dict[str, Any]:
        targets = self.store.list_quota_guard_targets(session["guard_id"])
        resumable = [
            target
            for target in targets
            if target["state"] == "HELD"
            and (target["goal_changed_by_guard"] or session["resume_non_goal_threads"])
        ]
        if not resumable:
            return self._schedule_check(session, state="ARMED", reason_code=None, now=now)
        current = self._schedule_check(session, state="RESUMING", reason_code=None, now=now)
        for target in resumable:
            try:
                self._resume_target(target, current, now)
            except Exception as exc:
                # No later target is resumed once one outcome is uncertain.
                latest = self.store.get_quota_guard_session(session["guard_id"])
                return self._mark_session_review(latest, self._reason(exc), now)
        latest_targets = self.store.list_quota_guard_targets(session["guard_id"])
        if any(target["state"] == "NEEDS_REVIEW" for target in latest_targets):
            return self._mark_session_review(
                self.store.get_quota_guard_session(session["guard_id"]),
                "RESUMPTION_UNCERTAIN",
                now,
            )
        latest = self.store.get_quota_guard_session(session["guard_id"])
        return self._schedule_check(latest, state="ARMED", reason_code=None, now=now)

    # ------------------------------ due cycle ---------------------------

    def _process_session(
        self,
        session: Dict[str, Any],
        snapshot: Optional[Dict[str, Any]],
        now: float,
        *,
        allow_resume: bool,
    ) -> Dict[str, Any]:
        if snapshot is not None and (
            snapshot.get("profile_key") != session["profile_key"]
            or snapshot.get("limit_id") != session["limit_id"]
        ):
            snapshot = None
        if session["state"] == "ARMED":
            decision = decide_trip(
                snapshot,
                session["threshold_remaining_percent"],
                now=now,
                max_snapshot_age_seconds=self.max_snapshot_age_seconds,
            )
            if not decision["contain"]:
                return self._schedule_check(session, reason_code=None, now=now)
            current = session
            if decision["signal_status"] == "fresh" and snapshot is not None:
                current = self.store.update_quota_guard_session(
                    session["guard_id"],
                    expected_revision=session["revision"],
                    expected_state="ARMED",
                    now=now,
                    stop_snapshot_hash=snapshot.get("snapshot_hash") or payload_hash(snapshot),
                    tripped_windows=decision["tripped_windows"],
                )
            current = self.store.transition_quota_guard_session(
                current["guard_id"],
                "STOPPING",
                expected_revision=current["revision"],
                expected_state="ARMED",
                now=now,
                reason_code=decision["reason_code"] or "SIGNAL_UNAVAILABLE",
            )
            return self._contain_all(current, now, decision["reason_code"] or "SIGNAL_UNAVAILABLE")
        if session["state"] != "HELD_QUOTA":
            return session
        # A missing/stale signal leaves a held guard held, but we still inspect
        # all targets so a manual restart is immediately re-contained.
        safe_to_resume = False
        stop_snapshot = None
        if session["stop_snapshot_hash"]:
            stop_snapshot = self.store.quota_snapshot_by_hash(session["stop_snapshot_hash"])
        decision = decide_resume(
            snapshot,
            stop_snapshot,
            session["threshold_remaining_percent"],
            session["resume_hysteresis_percent"],
            tripped_windows=session["tripped_windows"],
            now=now,
            max_snapshot_age_seconds=self.max_snapshot_age_seconds,
        )
        if not self._preflight_held_targets(session, now):
            return self._mark_session_review(
                self.store.get_quota_guard_session(session["guard_id"]),
                "RECONTAINMENT_UNCERTAIN",
                now,
            )
        paid_credit_safe = bool(
            snapshot is not None
            and snapshot.get("paid_credit_state") == "unavailable"
            and snapshot.get("spend_control_state") == "not_reached"
        )
        if decision["safe_to_resume"] and allow_resume and paid_credit_safe:
            safe_to_resume = True
        latest = self.store.get_quota_guard_session(session["guard_id"])
        if safe_to_resume:
            return self._resume_all(latest, now)
        return self._schedule_check(
            latest,
            state="HELD_QUOTA",
            reason_code=(
                (decision.get("reasons") or [None])[0]
                or ("CONTROLLER_RESUME_DISABLED" if not allow_resume else None)
                or ("PAID_CREDIT_SIGNAL_UNSAFE" if not paid_credit_safe else None)
                or "SIGNAL_UNAVAILABLE"
            ),
            now=now,
        )

    def one_due_cycle(
        self,
        *,
        now: Optional[float] = None,
        allow_resume: bool = True,
        signal_available: bool = True,
    ) -> Dict[str, Any]:
        observed_at = float(self.clock() if now is None else now)
        recovered = self.store.recover_quota_guard_pending(now=observed_at)
        snapshot = self.store.latest_snapshot() if signal_available else None
        sessions = (
            self.store.list_due_quota_guard_sessions(now=observed_at)
            if signal_available
            else self.store.list_quota_guard_sessions(states=("ARMED", "HELD_QUOTA"))
        )
        outcomes: List[Dict[str, Any]] = []
        for session in sessions:
            try:
                updated = self._process_session(
                    session,
                    snapshot,
                    observed_at,
                    allow_resume=allow_resume,
                )
            except SchedulerError as exc:
                updated = self._mark_session_review(
                    self.store.get_quota_guard_session(session["guard_id"]),
                    self._reason(exc),
                    observed_at,
                )
            if (
                updated["state"] != session["state"]
                or updated["reason_code"] != session["reason_code"]
            ):
                self._audit(
                    "quota_guard.transitioned",
                    guard_id=updated["guard_id"],
                    details={
                        "state": updated["state"],
                        "reason_code": updated["reason_code"],
                        "tripped_windows": updated["tripped_windows"],
                    },
                    now=observed_at,
                )
            outcomes.append(
                {
                    "guard_id": updated["guard_id"],
                    "state": updated["state"],
                    "reason_code": updated["reason_code"],
                    "tripped_windows": list(updated["tripped_windows"]),
                }
            )
        for guard_id in recovered:
            if not any(item["guard_id"] == guard_id for item in outcomes):
                current = self.store.get_quota_guard_session(guard_id)
                outcomes.append(
                    {
                        "guard_id": guard_id,
                        "state": current["state"],
                        "reason_code": current["reason_code"],
                        "tripped_windows": list(current["tripped_windows"]),
                    }
                )
        return {
            "checked_at": observed_at,
            "guards_checked": len(sessions),
            "recovered_guard_ids": recovered,
            "outcomes": outcomes,
        }

    cycle = one_due_cycle
    due_cycle = one_due_cycle


QuotaGuard = QuotaGuardCoordinator


__all__ = [
    "Adapter",
    "QuotaGuard",
    "QuotaGuardAdapter",
    "QuotaGuardCoordinator",
    "QUOTA_GUARD_SESSION_STATES",
    "QUOTA_GUARD_TARGET_STATES",
    "can_resume",
    "decide_resume",
    "decide_trip",
    "resume_decision",
    "should_trip",
    "trip_decision",
]
