"""Structured, redacted local notification events and sinks."""

import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .errors import SchedulerError
from .store import Store
from .util import canonical_json, new_id


EVENT_TYPES = frozenset(
    {"completion", "dispatch", "hold", "needs_review", "safety_stop", "signal_loss"}
)
SUBJECT_KINDS = frozenset({"controller", "job", "run", "service"})
SAFE_DETAIL_KEYS = frozenset(
    {
        "controller_mode",
        "failure_count",
        "job_state",
        "lease_recovered",
        "next_delay_seconds",
        "package_hash",
        "run_id",
        "run_state",
    }
)
_SAFE_STRING = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _safe_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_STRING.fullmatch(value):
        raise SchedulerError("NOTIFICATION_INVALID", "%s is not a safe identifier" % name)
    return value


def _redacted_details(value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SchedulerError("NOTIFICATION_INVALID", "Notification details must be an object")
    unknown = sorted(set(value) - SAFE_DETAIL_KEYS)
    if unknown:
        raise SchedulerError(
            "NOTIFICATION_REDACTION_REQUIRED",
            "Notification details contain fields outside the redacted contract",
            details={"unknown": unknown},
        )
    result: Dict[str, Any] = {}
    for key in sorted(value):
        item = value[key]
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str) and _SAFE_STRING.fullmatch(item):
            result[key] = item
        else:
            raise SchedulerError(
                "NOTIFICATION_REDACTION_REQUIRED",
                "Notification detail values must be bounded redacted scalars",
            )
    return result


class LocalJsonlSink:
    """Append owner-only redacted events to one local JSONL file."""

    kind = "local_jsonl"

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def emit(self, event: Dict[str, Any]) -> None:
        parent = self.path.parent
        parent_existed = parent.exists()
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed:
            parent.chmod(0o700)
        descriptor = os.open(
            str(self.path),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            payload = (canonical_json(event) + "\n").encode("utf-8")
            while payload:
                payload = payload[os.write(descriptor, payload) :]
        finally:
            os.close(descriptor)
        self.path.chmod(0o600)


class FakeNotificationSink:
    """Deterministic test sink with optional failure injection."""

    kind = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: List[Dict[str, Any]] = []

    def emit(self, event: Dict[str, Any]) -> None:
        if self.fail:
            raise OSError("injected notification failure")
        self.events.append(dict(event))


class NotificationBus:
    def __init__(
        self,
        store: Store,
        sink: Any,
        *,
        clock: Callable[[], float],
    ) -> None:
        self.store = store
        self.sink = sink
        self.clock = clock

    def emit(
        self,
        *,
        event_type: str,
        dedupe_key: str,
        subject_kind: str,
        subject_id: Optional[str] = None,
        reason_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if event_type not in EVENT_TYPES:
            raise SchedulerError("NOTIFICATION_INVALID", "The notification event type is unsupported")
        if subject_kind not in SUBJECT_KINDS:
            raise SchedulerError("NOTIFICATION_INVALID", "The notification subject type is unsupported")
        dedupe_key = _safe_identifier(dedupe_key, "dedupe_key")
        if subject_id is not None:
            subject_id = _safe_identifier(subject_id, "subject_id")
        if reason_code is not None:
            reason_code = _safe_identifier(reason_code, "reason_code")
        occurred_at = self.clock()
        event = {
            "schema_version": "1",
            "event_id": new_id("ntf"),
            "event_type": event_type,
            "occurred_at": occurred_at,
            "reason_code": reason_code,
            "subject": {"id": subject_id, "kind": subject_kind},
            "details": _redacted_details(details),
        }
        reserved = self.store.reserve_notification(
            event=event,
            dedupe_key=dedupe_key,
            sink_kind=self.sink.kind,
        )
        if reserved["deliver"]:
            try:
                self.sink.emit(reserved["event"])
            except Exception as exc:
                raise SchedulerError(
                    "NOTIFICATION_DELIVERY_FAILED",
                    "The local notification sink did not accept the redacted event",
                ) from exc
            self.store.mark_notification_delivered(
                event_id=reserved["event"]["event_id"],
                delivered_at=self.clock(),
            )
        return {
            "delivered": reserved["deliver"],
            "event": reserved["event"],
            "replayed": reserved["replayed"],
        }
