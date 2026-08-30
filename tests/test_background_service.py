import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from codex_work_scheduler.background import BackgroundSupervisor
from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.launchd import check_plist, render_plist, write_plist
from codex_work_scheduler.notifications import (
    FakeNotificationSink,
    LocalJsonlSink,
    NotificationBus,
)
from codex_work_scheduler.service import Controller
from codex_work_scheduler.store import Store
from codex_work_scheduler.validation import validate_config

from tests.helpers import FixedClock, approval, config, snapshot, work_package


class IncrementingProbe:
    def __init__(self, clock, value=None) -> None:
        self.clock = clock
        self.value = value or snapshot()
        self.calls = 0

    def read(self, *, profile_key, limit_id, account_fingerprint_key):
        self.calls += 1
        result = copy.deepcopy(self.value)
        result["observed_at"] = self.clock()
        result["profile_key"] = profile_key
        result["limit_id"] = limit_id
        result["five_hour"]["used_percent"] += self.calls / 1000.0
        result["five_hour"]["remaining_percent"] -= self.calls / 1000.0
        return result


class FailingProbe:
    def __init__(self, code="PROBE_UNAVAILABLE") -> None:
        self.code = code
        self.calls = 0

    def read(self, **_values):
        self.calls += 1
        raise SchedulerError(self.code, "injected probe failure", retryable=True)


class SequenceProbe:
    def __init__(self, values) -> None:
        self.values = [copy.deepcopy(value) for value in values]
        self.calls = 0

    def read(self, *, profile_key, limit_id, account_fingerprint_key):
        value = copy.deepcopy(self.values[min(self.calls, len(self.values) - 1)])
        self.calls += 1
        value["profile_key"] = profile_key
        value["limit_id"] = limit_id
        return value


class ImmediateSuccessRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, on_thread_started, on_started, **_values):
        self.calls += 1
        on_thread_started("thread-background")
        on_started("thread-background", "turn-background")
        return {
            "state": "succeeded",
            "stop_reason": None,
            "thread_id": "thread-background",
            "turn_id": "turn-background",
        }


class FailingRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, **_values):
        self.calls += 1
        raise SchedulerError("WORK_RUNNER_UNAVAILABLE", "injected runner failure")


class ShutdownRunner:
    def __init__(self) -> None:
        self.shutdown = None

    def run(self, *, safety_check, on_thread_started, on_started, **_values):
        on_thread_started("thread-shutdown")
        on_started("thread-shutdown", "turn-shutdown")
        self.shutdown()
        self.assert_safety = safety_check()
        return {
            "state": "interrupted",
            "stop_reason": "SAFETY_CHECK_FAILED",
            "thread_id": "thread-shutdown",
            "turn_id": "turn-shutdown",
        }


class LeaseOwnerLossRunner:
    def __init__(self, store) -> None:
        self.store = store
        self.calls = 0

    def run(self, *, safety_check, on_thread_started, on_started, **_values):
        self.calls += 1
        on_thread_started("thread-owner-loss")
        on_started("thread-owner-loss", "turn-owner-loss")
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE runs SET lease_owner = 'intruder' WHERE thread_id = ?",
                ("thread-owner-loss",),
            )
        self.safety_result = safety_check()
        return {
            "state": "interrupted",
            "stop_reason": "RUN_CLAIM_LOST",
            "thread_id": "thread-owner-loss",
            "turn_id": "turn-owner-loss",
        }


def background_config(database_path=":memory:", **overrides):
    value = config(database_path)
    value["background"] = {
        "enabled": True,
        "poll_interval_seconds": 10,
        "max_backoff_seconds": 40,
        "jitter_ratio": 0.2,
        "service_lease_seconds": 60,
        "notification_path": ".scheduler/notifications.jsonl",
    }
    value.update(overrides)
    return value


class BackgroundServiceTests(unittest.TestCase):
    def make_supervisor(self, value=None, *, probe=None, runner=None, sink=None, clock=None):
        clock = clock or FixedClock()
        value = value or background_config()
        store = Store(value["database_path"])
        controller = Controller(
            value,
            store,
            clock=clock,
            probe=probe or IncrementingProbe(clock),
            work_runner=runner,
        )
        sink = sink or FakeNotificationSink()
        bus = NotificationBus(store, sink, clock=clock)
        supervisor = BackgroundSupervisor(
            value,
            controller,
            store,
            bus,
            clock=clock,
            random_source=lambda: 0.5,
            wait=lambda _seconds: False,
            owner_id="owner-background-test",
        )
        return supervisor, controller, store, sink, clock

    @staticmethod
    def approve_and_resume(controller, package, prefix):
        controller.queue_propose(
            package,
            idempotency_key="%s-propose" % prefix,
            actor="agent",
        )
        stored = controller.queue_proposal_get(package["job"]["job_id"])
        controller.queue_approve(
            package["job"]["job_id"],
            approval(
                "queue.approve",
                stored["package"],
                "work.dispatch",
                approval_id="%s-approval" % prefix,
            ),
            idempotency_key="%s-approve" % prefix,
            actor="agent",
        )
        controller.probe_quota(
            idempotency_key="%s-probe" % prefix,
            actor="agent",
        )
        controller.resume(
            approval(
                "resume",
                {"action": "resume", "target_mode": "READY"},
                "control.local.write",
                approval_id="%s-resume" % prefix,
            ),
            idempotency_key="%s-resume-operation" % prefix,
            actor="agent",
        )

    def test_paused_service_polls_once_and_emits_deduplicated_hold(self):
        supervisor, controller, store, sink, _clock = self.make_supervisor()
        waits = []
        supervisor._wait = lambda seconds: waits.append(seconds) or False
        result = supervisor.run(max_cycles=2, install_signal_handlers=False)
        self.assertEqual(result["cycles"], 2)
        self.assertEqual(result["last_result"]["outcome"], "hold")
        self.assertEqual(controller.probe.calls, 2)
        self.assertEqual([event["event_type"] for event in sink.events], ["hold"])
        self.assertEqual(waits, [10.0])
        self.assertFalse(store.service_lease_status(now=controller.clock())["running"])

    def test_backoff_is_exponential_jittered_and_bounded(self):
        supervisor, _controller, _store, _sink, _clock = self.make_supervisor()
        self.assertEqual(supervisor._delay(0), 10.0)
        self.assertEqual(supervisor._delay(1), 10.0)
        self.assertEqual(supervisor._delay(2), 20.0)
        self.assertEqual(supervisor._delay(9), 40.0)
        supervisor.random_source = lambda: 0.0
        self.assertEqual(supervisor._delay(0), 8.0)
        supervisor.random_source = lambda: 1.0
        self.assertEqual(supervisor._delay(0), 12.0)
        self.assertEqual(supervisor._delay(9), 40.0)

    def test_probe_failure_blocks_and_uses_bounded_backoff(self):
        probe = FailingProbe()
        supervisor, controller, _store, sink, _clock = self.make_supervisor(probe=probe)
        result = supervisor.run(max_cycles=3, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["outcome"], "signal_loss")
        self.assertEqual(result["last_result"]["failure_count"], 3)
        self.assertEqual(result["last_result"]["next_delay_seconds"], 40.0)
        self.assertEqual(controller.status()["controller"]["mode"], "BLOCKED")
        self.assertEqual([event["event_type"] for event in sink.events], ["signal_loss"])

    def test_continuous_polling_detects_reset_without_resuming_or_dispatching(self):
        clock = FixedClock()
        before = snapshot(five_used=60.0)
        after = snapshot(
            five_used=5.0,
            five_reset=before["five_hour"]["resets_at"] + 3600,
        )
        after["observed_at"] += 1
        probe = SequenceProbe([before, after])
        supervisor, controller, _store, _sink, _clock = self.make_supervisor(
            probe=probe, clock=clock
        )
        result = supervisor.run(max_cycles=2, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["probe_reset_windows"], ["five_hour"])
        self.assertEqual(controller.status()["controller"]["mode"], "PAUSED")
        self.assertEqual(controller.store.counts(now=clock())["runs"], {})

    def test_stale_signal_and_account_change_stop_closed(self):
        stale = snapshot(observed_at=FixedClock()() - 1000)
        stale_supervisor, stale_controller, _store, _sink, _clock = self.make_supervisor(
            probe=SequenceProbe([stale])
        )
        stale_result = stale_supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(stale_result["last_result"]["reason_code"], "SIGNAL_STALE")
        self.assertEqual(stale_controller.status()["controller"]["mode"], "BLOCKED")

        supervisor, controller, _store, _sink, clock = self.make_supervisor()
        package = work_package("job-account-change")
        self.approve_and_resume(controller, package, "account-change")
        controller.probe = IncrementingProbe(
            clock,
            value=snapshot(account_fingerprint="2" * 64),
        )
        changed = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(changed["last_result"]["reason_code"], "PROFILE_MISMATCH")
        self.assertEqual(controller.queue_get("job-account-change")["state"], "approved")

    def test_singleton_service_lease_excludes_duplicates_and_recovers_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "state.sqlite")
            store = Store(database)
            first = store.acquire_service_lease(
                owner_id="owner-one", now=100.0, lease_seconds=60
            )
            with self.assertRaises(SchedulerError) as caught:
                Store(database).acquire_service_lease(
                    owner_id="owner-two", now=110.0, lease_seconds=60
                )
            self.assertEqual(caught.exception.code, "SERVICE_ALREADY_RUNNING")
            recovered = Store(database).acquire_service_lease(
                owner_id="owner-two", now=161.0, lease_seconds=60
            )
            self.assertNotEqual(first["lease_id"], recovered["lease_id"])
            self.assertTrue(recovered["recovered"])

    def test_crash_recovery_moves_expired_run_to_review_without_dispatch(self):
        runner = ImmediateSuccessRunner()
        supervisor, controller, _store, sink, clock = self.make_supervisor(runner=runner)
        package = work_package("job-crash-recovery")
        controller.queue_propose(package, idempotency_key="crash-propose", actor="agent")
        stored = controller.queue_proposal_get("job-crash-recovery")
        controller.queue_approve(
            "job-crash-recovery",
            approval(
                "queue.approve",
                stored["package"],
                "work.dispatch",
                approval_id="crash-approval",
            ),
            idempotency_key="crash-approve",
            actor="agent",
        )
        with controller.store.transaction() as connection:
            connection.execute("UPDATE jobs SET state = 'running' WHERE job_id = ?", ("job-crash-recovery",))
            connection.execute(
                """INSERT INTO runs(
                       run_id, job_id, kind, state, thread_id, turn_id, started_at,
                       updated_at, lease_expires_at
                   ) VALUES ('run-crash-recovery', 'job-crash-recovery', 'work',
                             'running', 'thread-old', 'turn-old', ?, ?, ?)""",
                (clock() - 120, clock() - 120, clock() - 1),
            )
        result = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["outcome"], "needs_review")
        self.assertEqual(controller.monitor_get("run-crash-recovery")["state"], "needs_review")
        self.assertEqual(controller.queue_get("job-crash-recovery")["state"], "needs_review")
        self.assertEqual(runner.calls, 0)
        self.assertEqual(sink.events[-1]["event_type"], "needs_review")

    def test_unattended_dispatch_selects_one_approved_highest_priority_package(self):
        runner = ImmediateSuccessRunner()
        supervisor, controller, _store, sink, _clock = self.make_supervisor(runner=runner)
        low = work_package("job-background-low", priority=10)
        high = work_package("job-background-high", priority=90)
        self.approve_and_resume(controller, low, "background-low")
        controller.queue_propose(high, idempotency_key="background-high-propose", actor="agent")
        stored = controller.queue_proposal_get("job-background-high")
        controller.queue_approve(
            "job-background-high",
            approval(
                "queue.approve",
                stored["package"],
                "work.dispatch",
                approval_id="background-high-approval",
            ),
            idempotency_key="background-high-approve",
            actor="agent",
        )
        result = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["outcome"], "completion")
        self.assertEqual(controller.queue_get("job-background-high")["state"], "succeeded")
        self.assertEqual(controller.queue_get("job-background-low")["state"], "approved")
        self.assertEqual(runner.calls, 1)
        self.assertEqual([event["event_type"] for event in sink.events], ["dispatch", "completion"])

    def test_failed_attempt_is_needs_review_and_never_retried(self):
        runner = FailingRunner()
        supervisor, controller, _store, sink, _clock = self.make_supervisor(runner=runner)
        package = work_package("job-no-retry")
        self.approve_and_resume(controller, package, "no-retry")
        result = supervisor.run(max_cycles=2, install_signal_handlers=False)
        self.assertEqual(runner.calls, 1)
        self.assertEqual(controller.queue_get("job-no-retry")["state"], "needs_review")
        self.assertEqual(controller.status()["controller"]["mode"], "PAUSED")
        self.assertIn("needs_review", [event["event_type"] for event in sink.events])
        self.assertEqual(result["cycles"], 2)

    def test_run_lease_owner_loss_stops_and_recovers_to_review_without_retry(self):
        clock = FixedClock()
        value = background_config()
        store = Store(value["database_path"])
        runner = LeaseOwnerLossRunner(store)
        controller = Controller(
            value,
            store,
            clock=clock,
            probe=IncrementingProbe(clock),
            work_runner=runner,
        )
        sink = FakeNotificationSink()
        supervisor = BackgroundSupervisor(
            value,
            controller,
            store,
            NotificationBus(store, sink, clock=clock),
            clock=clock,
            random_source=lambda: 0.5,
            wait=lambda _seconds: False,
            owner_id="owner-lease-test",
        )
        package = work_package("job-owner-loss")
        self.approve_and_resume(controller, package, "owner-loss")
        first = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertFalse(runner.safety_result)
        self.assertEqual(first["last_result"]["outcome"], "needs_review")
        self.assertEqual(controller.queue_get("job-owner-loss")["state"], "running")
        clock.advance(91)
        second = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(second["last_result"]["outcome"], "needs_review")
        self.assertEqual(controller.queue_get("job-owner-loss")["state"], "needs_review")
        self.assertEqual(runner.calls, 1)

    def test_shutdown_during_active_run_interrupts_and_requires_review(self):
        runner = ShutdownRunner()
        supervisor, controller, _store, _sink, _clock = self.make_supervisor(runner=runner)
        runner.shutdown = supervisor.request_shutdown
        package = work_package("job-safe-shutdown")
        self.approve_and_resume(controller, package, "safe-shutdown")
        result = supervisor.run(max_cycles=2, install_signal_handlers=False)
        self.assertFalse(runner.assert_safety)
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(controller.queue_get("job-safe-shutdown")["state"], "needs_review")
        self.assertEqual(controller.status()["controller"]["mode"], "PAUSED")

    def test_operator_pause_holds_and_operator_stop_exits_without_dispatch(self):
        for mode in ("pause", "stop"):
            with self.subTest(mode=mode):
                runner = ImmediateSuccessRunner()
                supervisor, controller, _store, sink, _clock = self.make_supervisor(runner=runner)
                package = work_package("job-operator-%s" % mode)
                self.approve_and_resume(controller, package, "operator-%s" % mode)
                getattr(controller, mode)(
                    reason_code="OPERATOR_%s" % mode.upper(),
                    idempotency_key="operator-%s-control" % mode,
                    actor="agent",
                )
                result = supervisor.run(max_cycles=1, install_signal_handlers=False)
                expected = "hold" if mode == "pause" else "stopped"
                self.assertEqual(result["last_result"]["outcome"], expected)
                self.assertEqual(runner.calls, 0)
                if mode == "pause":
                    self.assertEqual(sink.events[-1]["event_type"], "hold")

    def test_reserve_risk_and_explicit_credits_safety_stop_dispatch(self):
        for name, value, expected_reason in (
            ("reserve", snapshot(five_used=89.0), "FIVE_HOUR_RESERVE"),
            ("credits", snapshot(), "PAID_CREDITS_AVAILABLE"),
        ):
            with self.subTest(name=name):
                if name == "credits":
                    value["credit_signal"] = "present"
                    value["paid_credit_state"] = "available"
                probe = IncrementingProbe(FixedClock(), value=value)
                supervisor, controller, _store, _sink, _clock = self.make_supervisor(probe=probe)
                package = work_package("job-safety-%s" % name)
                self.approve_and_resume(controller, package, "safety-%s" % name)
                result = supervisor.run(max_cycles=1, install_signal_handlers=False)
                self.assertEqual(result["last_result"]["outcome"], "safety_stop")
                self.assertEqual(result["last_result"]["reason_code"], expected_reason)
                self.assertNotEqual(controller.queue_get(package["job"]["job_id"])["state"], "running")

    def test_audit_failure_stops_dispatch_without_extending_corrupt_chain(self):
        supervisor, controller, store, sink, _clock = self.make_supervisor()
        controller.pause(reason_code="AUDIT-SEED", idempotency_key="audit-seed", actor="agent")
        with store.transaction() as connection:
            connection.execute("UPDATE audit_events SET details_json = '{}' WHERE sequence = 1")
        event_count = store.verify_audit()["event_count"]
        result = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["reason_code"], "AUDIT_INVALID")
        self.assertEqual(store.verify_audit()["event_count"], event_count)
        self.assertEqual(sink.events[-1]["event_type"], "safety_stop")

    def test_notification_contract_redacts_rejects_and_deduplicates(self):
        store = Store(":memory:")
        sink = FakeNotificationSink()
        bus = NotificationBus(store, sink, clock=FixedClock())
        first = bus.emit(
            event_type="hold",
            dedupe_key="same-hold",
            subject_kind="controller",
            reason_code="PAUSED",
            details={"controller_mode": "PAUSED"},
        )
        second = bus.emit(
            event_type="hold",
            dedupe_key="same-hold",
            subject_kind="controller",
            reason_code="PAUSED",
            details={"controller_mode": "PAUSED"},
        )
        self.assertTrue(first["delivered"])
        self.assertFalse(second["delivered"])
        self.assertEqual(len(sink.events), 1)
        with self.assertRaises(SchedulerError) as caught:
            bus.emit(
                event_type="dispatch",
                dedupe_key="unsafe-event",
                subject_kind="job",
                subject_id="job-safe",
                details={"prompt": "secret objective"},
            )
        self.assertEqual(caught.exception.code, "NOTIFICATION_REDACTION_REQUIRED")
        self.assertNotIn("secret objective", json.dumps(store.list_notifications()))

    def test_notification_delivery_failure_blocks_before_dispatch(self):
        runner = ImmediateSuccessRunner()
        sink = FakeNotificationSink(fail=True)
        supervisor, controller, _store, _sink, _clock = self.make_supervisor(
            runner=runner, sink=sink
        )
        package = work_package("job-notification-failure")
        self.approve_and_resume(controller, package, "notification-failure")
        result = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["reason_code"], "NOTIFICATION_DELIVERY_FAILED")
        self.assertEqual(runner.calls, 0)
        self.assertEqual(controller.queue_get("job-notification-failure")["state"], "needs_review")

    def test_local_jsonl_sink_is_owner_only_and_contains_only_event_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events" / "notifications.jsonl"
            store = Store(":memory:")
            bus = NotificationBus(store, LocalJsonlSink(str(path)), clock=FixedClock())
            bus.emit(
                event_type="completion",
                dedupe_key="completion-local",
                subject_kind="run",
                subject_id="run-local",
                reason_code="WORK_COMPLETED",
                details={"run_state": "succeeded"},
            )
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            event = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event["event_type"], "completion")
            self.assertNotIn("prompt", event)

    def test_background_config_cross_field_constraints_fail_closed(self):
        value = background_config()
        value["background"]["max_backoff_seconds"] = 5
        with self.assertRaises(SchedulerError):
            validate_config(value)
        value = background_config()
        value["background"]["service_lease_seconds"] = 40
        with self.assertRaises(SchedulerError):
            validate_config(value)

    def test_launchd_render_and_check_are_disabled_and_install_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_path = root / "bin" / "codex"
            codex_path.parent.mkdir()
            codex_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            codex_path.chmod(0o700)
            config_path = root / "scheduler.json"
            config_path.write_text("{}", encoding="utf-8")
            content = render_plist(
                python_path=sys.executable,
                codex_path=str(codex_path),
                repository_path=str(Path(__file__).resolve().parents[1]),
                config_path=str(config_path),
                stdout_path=str(root / "service.stdout.jsonl"),
                stderr_path=str(root / "service.stderr.log"),
            )
            output = root / "staged" / "service.plist"
            rendered = write_plist(str(output), content)
            self.assertTrue(rendered["plist"]["disabled"])
            self.assertTrue(check_plist(str(output))["valid"])
            denied = Path.home() / "Library" / "LaunchAgents" / "test-service.plist"
            with self.assertRaises(SchedulerError) as caught:
                write_plist(str(denied), content)
            self.assertEqual(caught.exception.code, "LAUNCHD_INSTALL_DENIED")


if __name__ == "__main__":
    unittest.main()
