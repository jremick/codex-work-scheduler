"""Foreground background-service supervisor with fail-closed polling."""

import signal
import threading
from typing import Any, Callable, Dict, Optional

from .errors import SchedulerError
from .notifications import NotificationBus
from .service import Controller
from .store import Store
from .util import new_id, payload_hash


_HOLD_ISSUES = frozenset(
    {
        "CONTROLLER_NOT_READY",
        "DEPENDENCY_PENDING",
        "DISPATCH_DISABLED",
        "JOB_NOT_DISPATCHABLE",
        "LEASE_ACTIVE",
        "NO_ELIGIBLE_JOB",
        "NOT_BEFORE_PENDING",
        "QUOTA_GUARD_HELD",
        "RECONCILE_REQUIRED",
        "RUN_ACTIVE",
        "WORK_AUTHORIZATION_MISSING",
    }
)
_SIGNAL_ISSUES = frozenset(
    {
        "ACCOUNT_IDENTITY_UNAVAILABLE",
        "PROBE_PROTOCOL_ERROR",
        "PROBE_REJECTED",
        "PROBE_TIMEOUT",
        "PROBE_UNAVAILABLE",
        "PROFILE_MISMATCH",
        "SIGNAL_AMBIGUOUS",
        "SIGNAL_INVALID",
        "SIGNAL_MISSING",
        "SIGNAL_STALE",
    }
)


class BackgroundSupervisor:
    """Runs the local observe-select-dispatch loop in the foreground."""

    def __init__(
        self,
        config: Dict[str, Any],
        controller: Controller,
        store: Store,
        notifications: NotificationBus,
        *,
        clock: Callable[[], float],
        random_source: Callable[[], float],
        wait: Optional[Callable[[float], bool]] = None,
        owner_id: Optional[str] = None,
    ) -> None:
        self.config = config
        self.background = config["background"]
        self.controller = controller
        self.store = store
        self.notifications = notifications
        self.clock = clock
        self.random_source = random_source
        self.owner_id = owner_id or new_id("owner")
        self._stop_event = threading.Event()
        self._wait = wait or self._stop_event.wait
        self._lease: Optional[Dict[str, Any]] = None
        self._failure_count = 0
        self._incident_reason: Optional[str] = None
        self._incident_token: Optional[str] = None

    def status(self) -> Dict[str, Any]:
        self.controller._require_capability("background.read")
        return {
            "configured": {
                "enabled": self.background["enabled"],
                "jitter_ratio": self.background["jitter_ratio"],
                "max_backoff_seconds": self.background["max_backoff_seconds"],
                "poll_interval_seconds": self.background["poll_interval_seconds"],
                "service_lease_seconds": self.background["service_lease_seconds"],
            },
            "lease": self.store.service_lease_status(now=self.clock()),
        }

    def request_shutdown(self) -> None:
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def request_shutdown(_signum: int, _frame: Any) -> None:
            self.request_shutdown()

        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)

    def _audit(self, event_type: str, details: Dict[str, Any]) -> Optional[str]:
        with self.store.transaction() as connection:
            if not self.store.verify_audit_in(connection)["valid"]:
                return None
            return self.store.append_audit(
                connection,
                event_type=event_type,
                actor="background-supervisor",
                details=details,
                now=self.clock(),
            )

    def _acquire(self) -> Dict[str, Any]:
        if not self.background["enabled"]:
            raise SchedulerError(
                "BACKGROUND_DISABLED",
                "The foreground background service is not enabled in configuration",
            )
        self.controller._require_capability("background.run")
        self.controller._require_capability("notification.local.write")
        self._lease = self.store.acquire_service_lease(
            owner_id=self.owner_id,
            now=self.clock(),
            lease_seconds=self.background["service_lease_seconds"],
        )
        try:
            self._audit(
                "background.started",
                {
                    "lease_id": self._lease["lease_id"],
                    "recovered": self._lease["recovered"],
                },
            )
        except Exception:
            self.store.release_service_lease(
                lease_id=self._lease["lease_id"],
                owner_id=self.owner_id,
                now=self.clock(),
            )
            self._lease = None
            raise
        return self._lease

    def _heartbeat(self) -> None:
        if self._lease is None:
            raise SchedulerError("SERVICE_LEASE_MISSING", "The foreground service has no lease")
        self.store.renew_service_lease(
            lease_id=self._lease["lease_id"],
            owner_id=self.owner_id,
            now=self.clock(),
            lease_seconds=self.background["service_lease_seconds"],
        )

    def _release(self, reason_code: str) -> None:
        if self._lease is None:
            return
        released = self.store.release_service_lease(
            lease_id=self._lease["lease_id"],
            owner_id=self.owner_id,
            now=self.clock(),
        )
        try:
            self._audit(
                "background.stopped",
                {
                    "lease_id": self._lease["lease_id"],
                    "reason_code": reason_code,
                    "released": released,
                },
            )
        except Exception:
            pass
        self._lease = None

    def _delay(self, failure_count: int) -> float:
        base = float(self.background["poll_interval_seconds"])
        if failure_count:
            base *= 2 ** min(failure_count - 1, 16)
        bounded = min(base, float(self.background["max_backoff_seconds"]))
        ratio = float(self.background["jitter_ratio"])
        random_value = min(1.0, max(0.0, float(self.random_source())))
        jittered = bounded * (1.0 - ratio + (2.0 * ratio * random_value))
        return round(min(jittered, float(self.background["max_backoff_seconds"])), 3)

    @staticmethod
    def _dedupe(event_type: str, value: Dict[str, Any]) -> str:
        return "%s-%s" % (event_type, payload_hash(value)[:48])

    def _notify(self, **values: Any) -> Dict[str, Any]:
        return self.notifications.emit(**values)

    def _safety_stop(self, reason_code: str, *, failure_count: int) -> Dict[str, Any]:
        if self._incident_reason != reason_code:
            self._incident_reason = reason_code
            self._incident_token = new_id("incident")
        stopped = self.controller.background_safety_stop(
            reason_code=reason_code,
            actor="background-supervisor",
        )
        event_type = "signal_loss" if reason_code in _SIGNAL_ISSUES else "safety_stop"
        delay = self._delay(failure_count)
        try:
            self._notify(
                event_type=event_type,
                dedupe_key=self._dedupe(
                    event_type,
                    {"incident": self._incident_token, "reason_code": reason_code},
                ),
                subject_kind="controller",
                reason_code=reason_code,
                details={
                    "controller_mode": "BLOCKED",
                    "failure_count": failure_count,
                    "next_delay_seconds": delay,
                },
            )
        except SchedulerError:
            if reason_code != "NOTIFICATION_DELIVERY_FAILED":
                return self._safety_stop(
                    "NOTIFICATION_DELIVERY_FAILED",
                    failure_count=failure_count,
                )
        return {
            "controller": stopped,
            "failure_count": failure_count,
            "next_delay_seconds": delay,
            "outcome": event_type,
            "reason_code": reason_code,
        }

    def _contain_guards_without_signal(self) -> None:
        """Best-effort containment before the controller is safety-stopped."""
        if not self.config["quota_guard"]["enabled"]:
            return
        try:
            self.controller.quota_guard_cycle(
                signal_available=False,
                allow_resume=False,
            )
        except Exception:
            # The subsequent safety stop remains mandatory. The guard cycle
            # records NEEDS_REVIEW when it can establish a durable outcome.
            pass

    def run_once(self, *, cycle_number: int) -> Dict[str, Any]:
        if self._lease is None:
            raise SchedulerError("SERVICE_LEASE_MISSING", "The foreground service has no lease")
        self._heartbeat()
        if self._stop_event.is_set():
            return {"outcome": "shutdown", "next_delay_seconds": 0.0}
        if self.store.controller()["mode"] == "STOPPED":
            return {"outcome": "stopped", "next_delay_seconds": 0.0}
        if not self.store.integrity()["valid"]:
            self._failure_count += 1
            return self._safety_stop("DATABASE_INVALID", failure_count=self._failure_count)
        if not self.store.verify_audit()["valid"]:
            self._failure_count += 1
            return self._safety_stop("AUDIT_INVALID", failure_count=self._failure_count)

        recovered = self.controller.background_recover(actor="background-supervisor")
        if recovered["requires_review"]:
            self._failure_count = 0
            try:
                self._notify(
                    event_type="needs_review",
                    dedupe_key=self._dedupe(
                        "needs-review",
                        {
                            "leases": recovered["reconciled_lease_ids"],
                            "runs": recovered["reconciled_run_ids"],
                        },
                    ),
                    subject_kind="service",
                    reason_code="RECOVERY_REVIEW_REQUIRED",
                    details={"controller_mode": "PAUSED"},
                )
            except SchedulerError:
                return self._safety_stop("NOTIFICATION_DELIVERY_FAILED", failure_count=1)
            return {
                "next_delay_seconds": self._delay(0),
                "outcome": "needs_review",
                "recovery": recovered,
            }

        probe_key = "background-probe-%s" % payload_hash(
            {
                "cycle": cycle_number,
                "lease_id": self._lease["lease_id"],
                "owner_id": self.owner_id,
            }
        )[:48]
        try:
            probe_result = self.controller.probe_quota(
                idempotency_key=probe_key,
                actor="background-supervisor",
            )
        except SchedulerError as exc:
            self._failure_count += 1
            reason_code = exc.code if exc.code in _SIGNAL_ISSUES else "SIGNAL_INVALID"
            self._contain_guards_without_signal()
            return self._safety_stop(reason_code, failure_count=self._failure_count)
        except Exception:
            self._failure_count += 1
            self._contain_guards_without_signal()
            return self._safety_stop("PROBE_UNAVAILABLE", failure_count=self._failure_count)

        status = self.controller.status()
        signal_reasons = sorted(
            reason
            for reason in status["quota"]["decision"]["reasons"]
            if reason in _SIGNAL_ISSUES
        )
        if signal_reasons:
            self._failure_count += 1
            self._contain_guards_without_signal()
            return self._safety_stop(
                signal_reasons[0], failure_count=self._failure_count
            )
        self._failure_count = 0
        try:
            guard_result = self.controller.quota_guard_cycle()
        except SchedulerError as exc:
            return self._safety_stop(exc.code, failure_count=1)
        except Exception:
            return self._safety_stop("QUOTA_GUARD_UNAVAILABLE", failure_count=1)
        preflight = self.controller.dispatch_preflight()
        safety_issues = sorted(set(preflight["issues"]) - _HOLD_ISSUES)
        if safety_issues:
            return self._safety_stop(safety_issues[0], failure_count=1)
        self._incident_reason = None
        self._incident_token = None
        controller_mode = preflight["controller_mode"]
        if controller_mode == "STOPPED":
            return {"next_delay_seconds": 0.0, "outcome": "stopped"}
        if not preflight["safe_to_dispatch"]:
            issues = preflight["issues"] or ["NO_ELIGIBLE_JOB"]
            reason_code = issues[0]
            delay = self._delay(0)
            try:
                self._notify(
                    event_type="hold",
                    dedupe_key=self._dedupe(
                        "hold",
                        {
                            "controller_mode": controller_mode,
                            "reason_code": reason_code,
                            "updated_at": self.controller.store.controller()["updated_at"],
                        },
                    ),
                    subject_kind="controller",
                    reason_code=reason_code,
                    details={
                        "controller_mode": controller_mode,
                        "next_delay_seconds": delay,
                    },
                )
            except SchedulerError:
                return self._safety_stop("NOTIFICATION_DELIVERY_FAILED", failure_count=1)
            return {
                "quota_guard": guard_result,
                "issues": issues,
                "next_delay_seconds": delay,
                "outcome": "hold",
                "probe_reset_windows": probe_result["reset_windows"],
            }

        candidate = preflight["candidate"]
        job_id = candidate["job_id"]
        dispatch_key = "background-dispatch-%s" % payload_hash(
            {"job_id": job_id, "package_hash": candidate["package_hash"]}
        )[:48]
        try:
            self._notify(
                event_type="dispatch",
                dedupe_key=self._dedupe(
                    "dispatch",
                    {"job_id": job_id, "package_hash": candidate["package_hash"]},
                ),
                subject_kind="job",
                subject_id=job_id,
                reason_code="APPROVED_WORK_SELECTED",
                details={"job_state": "approved", "package_hash": candidate["package_hash"]},
            )
        except SchedulerError:
            self.controller.background_dispatch_failed(
                job_id=job_id,
                reason_code="NOTIFICATION_DELIVERY_FAILED",
                actor="background-supervisor",
            )
            return self._safety_stop("NOTIFICATION_DELIVERY_FAILED", failure_count=1)

        try:
            result = self.controller.dispatch_run(
                job_id=job_id,
                idempotency_key=dispatch_key,
                actor="background-supervisor",
                heartbeat=self._heartbeat,
                lease_owner=self.owner_id,
                shutdown_requested=self._stop_event.is_set,
            )
        except SchedulerError as exc:
            review = self.controller.background_dispatch_failed(
                job_id=job_id,
                reason_code=exc.code,
                actor="background-supervisor",
            )
            try:
                self._notify(
                    event_type="needs_review",
                    dedupe_key=self._dedupe(
                        "needs-review", {"job_id": job_id, "reason_code": exc.code}
                    ),
                    subject_kind="job",
                    subject_id=job_id,
                    reason_code=exc.code,
                    details={"controller_mode": "PAUSED", "job_state": "needs_review"},
                )
            except SchedulerError:
                return self._safety_stop("NOTIFICATION_DELIVERY_FAILED", failure_count=1)
            return {
                "dispatch_failure": review,
                "next_delay_seconds": self._delay(1),
                "outcome": "needs_review",
            }

        event_type = "completion" if result["state"] == "succeeded" else "needs_review"
        reason_code = result["stop_reason"] or "WORK_COMPLETED"
        try:
            self._notify(
                event_type=event_type,
                dedupe_key=self._dedupe(
                    event_type,
                    {"run_id": result["run_id"], "state": result["state"]},
                ),
                subject_kind="run",
                subject_id=result["run_id"],
                reason_code=reason_code,
                details={
                    "job_state": "succeeded" if result["state"] == "succeeded" else "needs_review",
                    "run_id": result["run_id"],
                    "run_state": result["state"],
                },
            )
        except SchedulerError:
            return self._safety_stop("NOTIFICATION_DELIVERY_FAILED", failure_count=1)
        return {
            "dispatch": result,
            "quota_guard": guard_result,
            "next_delay_seconds": self._delay(0),
            "outcome": event_type,
        }

    def run(self, *, max_cycles: int = 0, install_signal_handlers: bool = True) -> Dict[str, Any]:
        if isinstance(max_cycles, bool) or max_cycles < 0:
            raise SchedulerError("INVALID_ARGUMENT", "max_cycles must be zero or greater")
        if install_signal_handlers:
            self._install_signal_handlers()
        lease = self._acquire()
        cycles = 0
        last_result: Optional[Dict[str, Any]] = None
        stop_reason = "SHUTDOWN_REQUESTED"
        try:
            while not self._stop_event.is_set():
                if max_cycles and cycles >= max_cycles:
                    stop_reason = "MAX_CYCLES_REACHED"
                    break
                cycles += 1
                last_result = self.run_once(cycle_number=cycles)
                if last_result["outcome"] in {"shutdown", "stopped"}:
                    stop_reason = "CONTROLLER_STOPPED"
                    break
                if max_cycles and cycles >= max_cycles:
                    stop_reason = "MAX_CYCLES_REACHED"
                    break
                if self._wait(float(last_result["next_delay_seconds"])):
                    stop_reason = "SHUTDOWN_REQUESTED"
                    break
        finally:
            self._release(stop_reason)
        return {
            "cycles": cycles,
            "last_result": last_result,
            "lease_id": lease["lease_id"],
            "stopped": True,
            "stop_reason": stop_reason,
        }
