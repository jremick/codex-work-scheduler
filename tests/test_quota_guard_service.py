import unittest

from codex_work_scheduler.background import BackgroundSupervisor
from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.notifications import FakeNotificationSink, NotificationBus
from codex_work_scheduler.service import Controller
from codex_work_scheduler.store import Store
from codex_work_scheduler.util import payload_hash

from tests.helpers import FixedClock, FakeProbe, approval, config, snapshot, work_package


THREAD_ID = "thread-guard-service"
TURN_ID = "turn-guard-service"


class FakeThreadControl:
    def __init__(self) -> None:
        self.thread_status = "active"
        self.turn_status = "inProgress"
        self.goal_status = "active"
        self.goal_updated_at = 1
        self.calls = []

    def inventory_active_threads(self):
        return [
            {
                "thread_id": THREAD_ID,
                "status": self.thread_status,
                "active_turn_id": TURN_ID if self.turn_status == "inProgress" else None,
            }
        ]

    inventory = inventory_active_threads

    def read_thread(self, thread_id):
        self.calls.append(("read", thread_id))
        return {
            "thread_id": thread_id,
            "status": self.thread_status,
            "active_turn_id": TURN_ID if self.turn_status == "inProgress" else None,
            "turns": [{"turn_id": TURN_ID, "status": self.turn_status}],
        }

    def get_goal(self, thread_id):
        self.calls.append(("goal.get", thread_id))
        return {
            "thread_id": thread_id,
            "status": self.goal_status,
            "updated_at": self.goal_updated_at,
        }

    def set_goal_status(self, thread_id, status):
        self.calls.append(("goal.set", thread_id, status))
        self.goal_status = status
        self.goal_updated_at += 1
        if status == "paused":
            self.thread_status = "idle"
            self.turn_status = "interrupted"
        return {
            "thread_id": thread_id,
            "status": status,
            "updated_at": self.goal_updated_at,
        }

    def interrupt_turn(self, thread_id, turn_id):
        self.calls.append(("interrupt", thread_id, turn_id))
        self.thread_status = "idle"
        self.turn_status = "interrupted"
        return {"thread_id": thread_id, "turn_id": turn_id, "accepted": True}

    def resume_thread(self, thread_id):
        self.calls.append(("resume", thread_id))
        return {"thread_id": thread_id, "accepted": True}

    def start_continuation(self, thread_id):
        self.calls.append(("continue", thread_id))
        self.thread_status = "active"
        self.turn_status = "inProgress"
        return {"thread_id": thread_id, "turn_id": TURN_ID, "accepted": True}


class FailingProbe:
    def read(self, **_values):
        raise SchedulerError("PROBE_UNAVAILABLE", "injected failure")


class GuardHoldingRunner:
    def __init__(self, store, guard_id):
        self.store = store
        self.guard_id = guard_id
        self.started = False

    def run(self, *, safety_check, **_values):
        session = self.store.get_quota_guard_session(self.guard_id)
        self.store.transition_quota_guard_session(
            self.guard_id,
            "HELD_QUOTA",
            expected_revision=session["revision"],
            expected_state="ARMED",
            now=session["updated_at"] + 1,
            reason_code="TEST_CLAIM_START_RACE",
        )
        if safety_check():
            self.started = True
            return {
                "state": "succeeded",
                "stop_reason": None,
                "thread_id": "thread-should-not-start",
                "turn_id": "turn-should-not-start",
            }
        return {
            "state": "interrupted",
            "stop_reason": "SAFETY_CHECK_FAILED",
            "thread_id": None,
            "turn_id": None,
        }


class QuotaGuardServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.store = Store(":memory:")
        self.thread_control = FakeThreadControl()
        self.probe = FakeProbe(snapshot())
        value = config(":memory:")
        value["background"] = {**value["background"], "enabled": True}
        value["quota_guard"] = {**value["quota_guard"], "enabled": True}
        self.controller = Controller(
            value,
            self.store,
            clock=self.clock,
            probe=self.probe,
            thread_control=self.thread_control,
        )
        self.controller.probe_quota(idempotency_key="guard-initial-probe", actor="agent")
        self.plan = {
            "schema_version": "1",
            "threshold_remaining_percent": 10,
            "check_interval_seconds": 60,
            "target_thread_ids": [THREAD_ID],
            "resume_non_goal_threads": True,
        }

    def grant(self, approval_id="approval-quota-guard"):
        scope = self.controller._quota_guard_scope(self.plan)
        return approval(
            "quota-guard.arm",
            scope,
            "quota_guard.thread.control",
            approval_id=approval_id,
        )

    def arm(self):
        return self.controller.quota_guard_arm(
            self.plan,
            self.grant(),
            idempotency_key="guard-arm",
            actor="agent",
        )

    def test_inventory_is_transient_and_preflight_binds_the_exact_plan(self) -> None:
        inventory = self.controller.quota_guard_inventory()
        self.assertEqual(inventory["threads"][0]["thread_id"], THREAD_ID)
        self.assertEqual(self.store.list_quota_guard_sessions(), [])
        preflight = self.controller.preflight(
            action="quota-guard.arm", input_value=self.plan
        )
        self.assertEqual(
            preflight["approval_requirement"],
            {
                "action": "quota-guard.arm",
                "capability": "quota_guard.thread.control",
                "scope_hash": payload_hash(self.controller._quota_guard_scope(self.plan)),
            },
        )

    def test_arm_is_atomic_singleton_and_idempotent(self) -> None:
        first = self.arm()
        replay = self.arm()
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["guard_id"], replay["guard_id"])
        self.assertEqual(self.store.get_quota_guard_session(first["guard_id"])["state"], "ARMED")
        with self.assertRaises(SchedulerError) as caught:
            self.controller.quota_guard_arm(
                self.plan,
                self.grant("approval-quota-guard-two"),
                idempotency_key="guard-arm-two",
                actor="agent",
            )
        self.assertEqual(caught.exception.code, "QUOTA_GUARD_ACTIVE")

    def test_low_quota_pauses_goal_then_reset_restores_it(self) -> None:
        armed = self.arm()
        self.probe.value = snapshot(five_used=90, weekly_used=30)
        self.controller.probe_quota(idempotency_key="guard-low", actor="agent")
        held = self.controller.quota_guard_cycle()
        self.assertEqual(held["outcomes"][0]["state"], "HELD_QUOTA")
        self.assertEqual(self.thread_control.goal_status, "paused")
        self.assertNotIn("interrupt", [call[0] for call in self.thread_control.calls])
        self.assertEqual(self.store.controller()["mode"], "PAUSED")

        self.clock.advance(60)
        self.probe.value = snapshot(
            observed_at=self.clock(),
            five_used=20,
            weekly_used=20,
            five_reset=self.clock() + 7200,
            weekly_reset=self.clock() + 8 * 86400,
        )
        self.controller.probe_quota(idempotency_key="guard-reset", actor="agent")
        resumed = self.controller.quota_guard_cycle()
        self.assertEqual(resumed["outcomes"][0]["state"], "ARMED")
        self.assertEqual(self.thread_control.goal_status, "active")
        target = self.store.get_quota_guard_target(armed["guard_id"], THREAD_ID)
        self.assertEqual(target["state"], "RESUMED")

    def test_disarm_never_resumes_a_held_target(self) -> None:
        armed = self.arm()
        self.probe.value = snapshot(five_used=90, weekly_used=30)
        self.controller.probe_quota(idempotency_key="guard-low-disarm", actor="agent")
        self.controller.quota_guard_cycle()
        self.assertEqual(self.thread_control.goal_status, "paused")
        result = self.controller.quota_guard_disarm(
            armed["guard_id"],
            idempotency_key="guard-disarm",
            actor="agent",
        )
        self.assertEqual(result["state"], "DISARMED")
        self.assertEqual(self.thread_control.goal_status, "paused")

    def test_singleton_background_cycle_contains_before_dispatch(self) -> None:
        armed = self.arm()
        self.controller.resume(
            approval(
                "resume",
                {"action": "resume", "target_mode": "READY"},
                "control.local.write",
                approval_id="approval-controller-ready",
            ),
            idempotency_key="controller-ready",
            actor="agent",
        )
        self.assertEqual(self.store.controller()["mode"], "READY")
        self.probe.value = snapshot(five_used=90, weekly_used=30)
        supervisor = BackgroundSupervisor(
            self.controller.config,
            self.controller,
            self.store,
            NotificationBus(self.store, FakeNotificationSink(), clock=self.clock),
            clock=self.clock,
            random_source=lambda: 0.5,
            wait=lambda _seconds: False,
            owner_id="quota-guard-service-owner",
        )
        result = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["outcome"], "hold")
        self.assertEqual(
            self.store.get_quota_guard_session(armed["guard_id"])["state"],
            "HELD_QUOTA",
        )
        self.assertEqual(result["last_result"]["quota_guard"]["guards_checked"], 1)
        self.assertIsNone(result["last_result"].get("dispatch"))
        self.assertEqual(self.store.controller()["mode"], "PAUSED")

    def test_probe_failure_contains_armed_targets_before_safety_stop(self) -> None:
        armed = self.arm()
        self.controller.probe = FailingProbe()
        supervisor = BackgroundSupervisor(
            self.controller.config,
            self.controller,
            self.store,
            NotificationBus(self.store, FakeNotificationSink(), clock=self.clock),
            clock=self.clock,
            random_source=lambda: 0.5,
            wait=lambda _seconds: False,
            owner_id="quota-guard-probe-failure-owner",
        )
        result = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["outcome"], "signal_loss")
        self.assertEqual(
            self.store.get_quota_guard_session(armed["guard_id"])["state"],
            "HELD_QUOTA",
        )
        self.assertEqual(self.thread_control.goal_status, "paused")
        self.assertEqual(self.store.controller()["mode"], "BLOCKED")

    def test_probe_failure_forces_containment_before_next_guard_interval(self) -> None:
        armed = self.arm()
        healthy = self.controller.quota_guard_cycle()
        self.assertEqual(healthy["outcomes"][0]["state"], "ARMED")
        self.clock.advance(1)
        self.controller.probe = FailingProbe()
        supervisor = BackgroundSupervisor(
            self.controller.config,
            self.controller,
            self.store,
            NotificationBus(self.store, FakeNotificationSink(), clock=self.clock),
            clock=self.clock,
            random_source=lambda: 0.5,
            wait=lambda _seconds: False,
            owner_id="quota-guard-between-checks-owner",
        )
        result = supervisor.run(max_cycles=1, install_signal_handlers=False)
        self.assertEqual(result["last_result"]["outcome"], "signal_loss")
        self.assertEqual(
            self.store.get_quota_guard_session(armed["guard_id"])["state"],
            "HELD_QUOTA",
        )
        self.assertEqual(self.thread_control.goal_status, "paused")

    def test_blocked_or_stopped_controller_never_resumes_targets(self) -> None:
        armed = self.arm()
        self.probe.value = snapshot(five_used=90, weekly_used=30)
        self.controller.probe_quota(idempotency_key="guard-low-modes", actor="agent")
        self.controller.quota_guard_cycle()
        self.clock.advance(60)
        self.probe.value = snapshot(
            observed_at=self.clock(),
            five_used=20,
            weekly_used=20,
            five_reset=self.clock() + 7200,
            weekly_reset=self.clock() + 8 * 86400,
        )
        self.controller.probe_quota(idempotency_key="guard-reset-modes", actor="agent")
        with self.store.transaction() as connection:
            self.store.set_controller(
                connection,
                mode="BLOCKED",
                reason_code="TEST_BLOCKED",
                now=self.clock(),
            )
        blocked = self.controller.quota_guard_cycle()
        self.assertEqual(blocked["outcomes"][0]["state"], "HELD_QUOTA")
        self.assertEqual(self.thread_control.goal_status, "paused")
        self.clock.advance(60)
        with self.store.transaction() as connection:
            self.store.set_controller(
                connection,
                mode="STOPPED",
                reason_code="TEST_STOPPED",
                now=self.clock(),
            )
        stopped = self.controller.quota_guard_cycle()
        self.assertEqual(stopped["skipped"], "CONTROLLER_STOPPED")
        self.assertEqual(self.thread_control.goal_status, "paused")
        self.assertEqual(
            self.store.get_quota_guard_target(armed["guard_id"], THREAD_ID)["state"],
            "HELD",
        )

    def test_dispatch_start_rechecks_guard_after_claim(self) -> None:
        armed = self.arm()
        package = work_package("job-guard-claim-race")
        self.controller.queue_propose(
            package,
            idempotency_key="guard-race-propose",
            actor="agent",
        )
        stored = self.controller.queue_proposal_get("job-guard-claim-race")
        self.controller.queue_approve(
            "job-guard-claim-race",
            approval(
                "queue.approve",
                stored["package"],
                "work.dispatch",
                approval_id="guard-race-queue-approval",
            ),
            idempotency_key="guard-race-approve",
            actor="agent",
        )
        self.controller.resume(
            approval(
                "resume",
                {"action": "resume", "target_mode": "READY"},
                "control.local.write",
                approval_id="guard-race-ready-approval",
            ),
            idempotency_key="guard-race-ready",
            actor="agent",
        )
        runner = GuardHoldingRunner(self.store, armed["guard_id"])
        self.controller.work_runner = runner
        self.clock.advance(1)
        self.probe.value = snapshot(observed_at=self.clock(), five_used=20, weekly_used=30)
        result = self.controller.dispatch_run(
            job_id="job-guard-claim-race",
            idempotency_key="guard-race-dispatch",
            actor="agent",
        )
        self.assertFalse(runner.started)
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["stop_reason"], "SAFETY_CHECK_FAILED")


if __name__ == "__main__":
    unittest.main()
