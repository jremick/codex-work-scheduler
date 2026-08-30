import copy
import json
import sys
import time
import unittest
from pathlib import Path

from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.live_test import CANARY_PROMPT, CodexLiveTestRunner
from codex_work_scheduler.monitor import CodexThreadMonitor
from codex_work_scheduler.probe import CodexAppServerProbe
from codex_work_scheduler.service import Controller
from codex_work_scheduler.store import Store
from codex_work_scheduler.work_runner import CodexWorkRunner

from tests.helpers import FakeProbe, FixedClock, NOW, approval, config, snapshot, work_package


FIXTURE = Path(__file__).parent / "fixtures" / "fake_app_server.py"


class SequenceProbe:
    def __init__(self, values):
        self.values = [copy.deepcopy(value) for value in values]
        self.index = 0

    def read(self, *, profile_key, limit_id, account_fingerprint_key):
        value = copy.deepcopy(self.values[min(self.index, len(self.values) - 1)])
        self.index += 1
        value["profile_key"] = profile_key
        value["limit_id"] = limit_id
        return value


class ImmediateSuccessRunner:
    def run(self, *, safety_check, on_started, **_kwargs):
        if not safety_check():
            return {
                "state": "blocked",
                "stop_reason": "SAFETY_CHECK_FAILED",
                "thread_id": None,
                "turn_id": None,
            }
        on_started("thread-stub", "turn-stub")
        return {
            "state": "succeeded",
            "stop_reason": None,
            "thread_id": "thread-stub",
            "turn_id": "turn-stub",
        }


class BacklogLiveMonitorTests(unittest.TestCase):
    def _approve_package(self, controller, package, prefix):
        controller.queue_propose(
            package, idempotency_key="%s-propose" % prefix, actor="agent"
        )
        stored = controller.queue_proposal_get(package["job"]["job_id"])
        return controller.queue_approve(
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

    def test_backlog_proposal_requires_scoped_approval_before_queue_admission(self) -> None:
        clock = FixedClock()
        controller = Controller(config(":memory:"), Store(":memory:"), clock=clock)
        package = work_package("job-backlog")
        proposed = controller.queue_propose(
            package, idempotency_key="propose-backlog", actor="agent"
        )
        self.assertEqual(proposed["state"], "proposed")
        self.assertEqual(controller.queue_list()["jobs"], [])
        stored = controller.queue_proposal_get("job-backlog")
        self.assertTrue(Path(stored["package"]["execution"]["cwd"]).is_absolute())
        prepared = controller.approval_prepare(
            action="queue.approve",
            input_value=package,
            requested_approver="operator",
            suggested_ttl_seconds=600,
        )
        self.assertEqual(prepared["approval_request"]["scope_hash"], stored["package_hash"])
        grant = approval(
            "queue.approve",
            stored["package"],
            "work.dispatch",
            approval_id="approve-backlog",
        )
        approved = controller.queue_approve(
            "job-backlog",
            grant,
            idempotency_key="approve-backlog-operation",
            actor="agent",
        )
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(controller.queue_get("job-backlog")["state"], "approved")
        self.assertEqual(controller.queue_proposal_get("job-backlog")["state"], "approved")
        audit_json = json.dumps(controller.audit_list(limit=20))
        self.assertNotIn(package["objective"], audit_json)

    def test_backlog_rejects_cwd_outside_workspace_roots(self) -> None:
        controller = Controller(config(":memory:"), Store(":memory:"), clock=FixedClock())
        package = work_package("job-escape")
        package["execution"]["cwd"] = "/"
        with self.assertRaises(SchedulerError) as caught:
            controller.queue_propose(package, idempotency_key="escape", actor="agent")
        self.assertEqual(caught.exception.code, "PATH_DENIED")

    def test_queue_approval_preflight_grants_exact_work_dispatch_capability(self) -> None:
        controller = Controller(config(":memory:"), Store(":memory:"), clock=FixedClock())
        package = work_package("job-approval-scope")
        prepared = controller.approval_prepare(
            action="queue.approve",
            input_value=package,
            requested_approver="operator",
            suggested_ttl_seconds=600,
        )
        self.assertEqual(prepared["approval_request"]["capability"], "work.dispatch")
        self.assertEqual(prepared["approval_template"]["capabilities"], ["work.dispatch"])

    def test_dependencies_and_not_before_hold_work_until_eligible(self) -> None:
        clock = FixedClock()
        controller = Controller(
            config(":memory:"),
            Store(":memory:"),
            clock=clock,
            probe=FakeProbe(snapshot()),
        )
        controller.probe_quota(idempotency_key="backlog-probe", actor="agent")
        resume_scope = {"action": "resume", "target_mode": "READY"}
        controller.resume(
            approval(
                "resume",
                resume_scope,
                "control.local.write",
                approval_id="backlog-resume",
            ),
            idempotency_key="backlog-resume-operation",
            actor="agent",
        )
        packages = [
            work_package("scheduled", priority=100, not_before=NOW + 1000),
            work_package("child", priority=90, dependencies=["dependency"]),
            work_package("dependency", priority=10),
        ]
        for index, package in enumerate(packages):
            job_id = package["job"]["job_id"]
            controller.queue_propose(
                package, idempotency_key="propose-%d" % index, actor="agent"
            )
            stored = controller.queue_proposal_get(job_id)
            controller.queue_approve(
                job_id,
                approval(
                    "queue.approve",
                    stored["package"],
                    "work.dispatch",
                    approval_id="approve-%d" % index,
                ),
                idempotency_key="approve-operation-%d" % index,
                actor="agent",
            )
        first = controller.tick(idempotency_key="backlog-tick-1", actor="agent")
        self.assertEqual(first["job_id"], "dependency")
        self.assertEqual(controller.queue_get("scheduled")["state"], "held_schedule")
        self.assertEqual(controller.queue_get("child")["state"], "held_dependency")
        second = controller.tick(idempotency_key="backlog-tick-2", actor="agent")
        self.assertEqual(second["job_id"], "child")

    def test_live_runner_uses_fixed_canary_and_completes_against_fake_server(self) -> None:
        command = [sys.executable, str(FIXTURE), "complete"]
        runner = CodexLiveTestRunner(command=command)
        started = []
        result = runner.run(
            cwd=str(Path.cwd()),
            model="gpt-5.4-mini",
            effort="low",
            max_runtime_seconds=10,
            poll_interval_seconds=1,
            safety_check=lambda: True,
            on_started=lambda thread_id, turn_id: started.append((thread_id, turn_id)),
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(started, [("thread-fixture", "turn-fixture")])
        self.assertIn("Do not call tools", CANARY_PROMPT)

    def test_live_runner_interrupts_when_a_safety_refresh_fails(self) -> None:
        command = [sys.executable, str(FIXTURE), "hold"]
        runner = CodexLiveTestRunner(command=command)
        checks = {"count": 0}

        def safety() -> bool:
            checks["count"] += 1
            return checks["count"] <= 2

        result = runner.run(
            cwd=str(Path.cwd()),
            model="gpt-5.4-mini",
            effort="low",
            max_runtime_seconds=10,
            poll_interval_seconds=1,
            safety_check=safety,
            on_started=lambda _thread_id, _turn_id: None,
        )
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["stop_reason"], "SAFETY_CHECK_FAILED")

    def test_monitor_returns_only_task_and_turn_state(self) -> None:
        monitor = CodexThreadMonitor(command=[sys.executable, str(FIXTURE), "complete"])
        result = monitor.read("thread-fixture")
        self.assertEqual(result["thread_status"], "idle")
        self.assertEqual(result["latest_turn_status"], "completed")
        self.assertNotIn("items", json.dumps(result))
        self.assertNotIn("output", json.dumps(result))

    def test_controller_live_canary_is_approval_gated_and_audited(self) -> None:
        now = time.time()
        live_config = config(":memory:", workspace_roots=[str(Path.cwd())])
        store = Store(":memory:")
        probe = CodexAppServerProbe(clock=time.time)
        probe.COMMAND = (sys.executable, str(FIXTURE), "complete")
        runner = CodexLiveTestRunner(command=[sys.executable, str(FIXTURE), "complete"])
        controller = Controller(
            live_config,
            store,
            clock=time.time,
            probe=probe,
            live_test_runner=runner,
        )
        controller.probe_quota(idempotency_key="initial-live-probe", actor="agent")
        preflight = controller.live_test_preflight()
        self.assertTrue(preflight["safe_to_dispatch"])
        grant = approval(
            "live-test.run",
            controller._live_test_scope(),
            "live_test.dispatch",
            approval_id="approve-live-canary",
            now=now,
        )
        result = controller.live_test_run(
            grant, idempotency_key="live-canary-operation", actor="agent"
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(controller.monitor_get(result["run_id"])["state"], "succeeded")
        self.assertTrue(controller.audit_verify()["valid"])

    def test_live_preflight_stops_when_paid_credit_state_is_unknown(self) -> None:
        unknown = snapshot()
        unknown["credit_signal"] = "unknown"
        unknown["paid_credit_state"] = "unknown"
        unknown["spend_control_state"] = "unknown"
        controller = Controller(
            config(":memory:"),
            Store(":memory:"),
            clock=FixedClock(),
            probe=FakeProbe(unknown),
        )
        controller.probe_quota(idempotency_key="unknown-credit-probe", actor="agent")
        result = controller.live_test_preflight()
        self.assertFalse(result["safe_to_dispatch"])
        self.assertIn("PAID_CREDIT_SIGNAL_UNVERIFIED", result["issues"])

    def test_operator_attested_subscription_only_mode_allows_unknown_credit_metadata(self) -> None:
        unknown = snapshot()
        unknown["credit_signal"] = "unknown"
        unknown["paid_credit_state"] = "unknown"
        unknown["spend_control_state"] = "unknown"
        value = config(":memory:")
        value["dispatch"]["credit_verification_mode"] = (
            "operator_attested_subscription_only"
        )
        controller = Controller(
            value,
            Store(":memory:"),
            clock=FixedClock(),
            probe=FakeProbe(unknown),
        )
        self._approve_package(
            controller,
            work_package("job-attested-credit-policy"),
            "attested-credit-policy",
        )
        controller.probe_quota(idempotency_key="attested-credit-probe", actor="agent")
        controller.resume(
            approval(
                "resume",
                {"action": "resume", "target_mode": "READY"},
                "control.local.write",
                approval_id="attested-credit-resume",
            ),
            idempotency_key="attested-credit-resume-operation",
            actor="agent",
        )
        result = controller.dispatch_preflight(job_id="job-attested-credit-policy")
        self.assertTrue(result["safe_to_dispatch"])
        self.assertEqual(
            result["credit_verification_mode"],
            "operator_attested_subscription_only",
        )

    def test_operator_attestation_never_overrides_contrary_credit_evidence(self) -> None:
        value = config(":memory:")
        value["dispatch"]["credit_verification_mode"] = (
            "operator_attested_subscription_only"
        )
        controller = Controller(value, Store(":memory:"), clock=FixedClock())
        available = snapshot()
        available["credit_signal"] = "present"
        available["paid_credit_state"] = "available"
        self.assertEqual(
            controller._paid_credit_issues(available),
            ["PAID_CREDITS_AVAILABLE"],
        )
        reached = snapshot()
        reached["spend_control_state"] = "reached"
        self.assertEqual(
            controller._paid_credit_issues(reached),
            ["SPEND_CONTROL_REACHED"],
        )

    def test_work_runner_completes_only_after_capability_inventory(self) -> None:
        runner = CodexWorkRunner(command=[sys.executable, str(FIXTURE), "complete"])
        started = []
        result = runner.run(
            objective="Perform the approved local work package.",
            cwd=str(Path.cwd()),
            model="gpt-5.4-mini",
            effort="low",
            sandbox="workspace_write",
            max_runtime_seconds=10,
            poll_interval_seconds=1,
            safety_check=lambda: True,
            on_thread_started=lambda _thread_id: None,
            on_started=lambda thread_id, turn_id: started.append((thread_id, turn_id)),
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(started, [("thread-fixture", "turn-fixture")])

    def test_work_runner_blocks_hooks_apps_and_mcp_before_turn(self) -> None:
        expected = {
            "hook": "MANAGED_HOOKS_ENABLED",
            "app": "APPS_ENABLED",
            "mcp": "MCP_SERVERS_ENABLED",
        }
        for mode, reason in expected.items():
            with self.subTest(mode=mode):
                runner = CodexWorkRunner(command=[sys.executable, str(FIXTURE), mode])
                result = runner.run(
                    objective="Approved fixture work.",
                    cwd=str(Path.cwd()),
                    model="gpt-5.4-mini",
                    effort="low",
                    sandbox="read_only",
                    max_runtime_seconds=10,
                    poll_interval_seconds=1,
                    safety_check=lambda: True,
                    on_thread_started=lambda _thread_id: None,
                    on_started=lambda _thread_id, _turn_id: self.fail("turn started"),
                )
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["stop_reason"], reason)
                self.assertIsNone(result["turn_id"])

    def test_work_runner_applies_thread_scoped_capability_overrides(self) -> None:
        runner = CodexWorkRunner(command=[sys.executable, str(FIXTURE), "overrides"])
        result = runner.run(
            objective="Approved fixture work.",
            cwd=str(Path.cwd()),
            model="gpt-5.6-luna",
            effort="low",
            sandbox="workspace_write",
            max_runtime_seconds=10,
            poll_interval_seconds=1,
            safety_check=lambda: True,
            on_thread_started=lambda _thread_id: None,
            on_started=lambda _thread_id, _turn_id: None,
        )
        self.assertEqual(result["state"], "succeeded")

    def test_work_runner_reports_the_rejected_method_without_server_details(self) -> None:
        runner = CodexWorkRunner(
            command=[sys.executable, str(FIXTURE), "reject-thread-start"]
        )
        with self.assertRaises(SchedulerError) as caught:
            runner.run(
                objective="Approved fixture work.",
                cwd=str(Path.cwd()),
                model="gpt-5.6-luna",
                effort="low",
                sandbox="read_only",
                max_runtime_seconds=10,
                poll_interval_seconds=1,
                safety_check=lambda: True,
                on_thread_started=lambda _thread_id: None,
                on_started=lambda _thread_id, _turn_id: None,
            )
        self.assertEqual(
            caught.exception.code,
            "WORK_METHOD_REJECTED_THREAD_START",
        )
        self.assertEqual(caught.exception.details, {})

    def test_work_runner_interrupts_when_periodic_quota_check_fails(self) -> None:
        runner = CodexWorkRunner(command=[sys.executable, str(FIXTURE), "hold"])
        checks = {"count": 0}

        def safety() -> bool:
            checks["count"] += 1
            return checks["count"] <= 3

        result = runner.run(
            objective="Approved fixture work.",
            cwd=str(Path.cwd()),
            model="gpt-5.4-mini",
            effort="low",
            sandbox="workspace_write",
            max_runtime_seconds=10,
            poll_interval_seconds=1,
            safety_check=safety,
            on_thread_started=lambda _thread_id: None,
            on_started=lambda _thread_id, _turn_id: None,
        )
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(result["stop_reason"], "SAFETY_CHECK_FAILED")

    def test_approved_package_dispatches_and_monitors_with_fake_app_server(self) -> None:
        now = time.time()
        live_config = config(":memory:", workspace_roots=[str(Path.cwd())])
        store = Store(":memory:")
        probe = CodexAppServerProbe(clock=time.time)
        probe.COMMAND = (sys.executable, str(FIXTURE), "complete")
        controller = Controller(
            live_config,
            store,
            clock=time.time,
            probe=probe,
            work_runner=CodexWorkRunner(
                command=[sys.executable, str(FIXTURE), "complete"]
            ),
            monitor=CodexThreadMonitor(
                command=[sys.executable, str(FIXTURE), "complete"]
            ),
        )
        package = work_package("job-dispatch")
        controller.queue_propose(package, idempotency_key="dispatch-propose", actor="agent")
        stored = controller.queue_proposal_get("job-dispatch")
        controller.queue_approve(
            "job-dispatch",
            approval(
                "queue.approve",
                stored["package"],
                "work.dispatch",
                approval_id="dispatch-approval",
                now=now,
            ),
            idempotency_key="dispatch-approve-operation",
            actor="agent",
        )
        controller.probe_quota(idempotency_key="dispatch-initial-probe", actor="agent")
        resume_scope = {"action": "resume", "target_mode": "READY"}
        controller.resume(
            approval(
                "resume",
                resume_scope,
                "control.local.write",
                approval_id="dispatch-resume",
                now=now,
            ),
            idempotency_key="dispatch-resume-operation",
            actor="agent",
        )
        preflight = controller.dispatch_preflight(job_id="job-dispatch")
        self.assertTrue(preflight["safe_to_dispatch"])
        result = controller.dispatch_run(
            job_id="job-dispatch",
            idempotency_key="dispatch-run-operation",
            actor="agent",
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(controller.queue_get("job-dispatch")["state"], "succeeded")
        self.assertEqual(controller.monitor_get(result["run_id"])["kind"], "work")
        refreshed = controller.monitor_refresh(
            result["run_id"],
            idempotency_key="dispatch-monitor-refresh",
            actor="agent",
        )
        self.assertEqual(refreshed["state"], "succeeded")
        replay = controller.dispatch_run(
            job_id="job-dispatch",
            idempotency_key="dispatch-run-operation",
            actor="agent",
        )
        self.assertTrue(replay["idempotent_replay"])
        audit_json = json.dumps(controller.audit_list(limit=100))
        self.assertNotIn(package["objective"], audit_json)

    def test_legacy_simulation_job_lacks_live_dispatch_authority(self) -> None:
        clock = FixedClock()
        controller = Controller(
            config(":memory:"),
            Store(":memory:"),
            clock=clock,
            probe=FakeProbe(snapshot()),
        )
        controller.probe_quota(idempotency_key="legacy-probe", actor="agent")
        resume_scope = {"action": "resume", "target_mode": "READY"}
        controller.resume(
            approval(
                "resume",
                resume_scope,
                "control.local.write",
                approval_id="legacy-resume",
            ),
            idempotency_key="legacy-resume-operation",
            actor="agent",
        )
        legacy_job = work_package("legacy-job")["job"]
        controller.queue_submit(
            legacy_job,
            approval(
                "queue.submit",
                legacy_job,
                "queue.local.write",
                approval_id="legacy-submit",
            ),
            idempotency_key="legacy-submit-operation",
            actor="agent",
        )
        preflight = controller.dispatch_preflight(job_id="legacy-job")
        self.assertFalse(preflight["safe_to_dispatch"])
        self.assertIn("WORK_AUTHORIZATION_MISSING", preflight["issues"])

    def test_post_run_usage_overrun_pauses_and_requires_review(self) -> None:
        clock = FixedClock()
        values = []
        for index, five_used in enumerate((20.0, 21.0, 30.0)):
            value = snapshot(five_used=five_used)
            values.append(value)
        controller = Controller(
            config(":memory:"),
            Store(":memory:"),
            clock=clock,
            probe=SequenceProbe(values),
            work_runner=ImmediateSuccessRunner(),
        )
        package = work_package("job-overrun-live")
        self._approve_package(controller, package, "overrun-live")
        controller.probe_quota(idempotency_key="overrun-initial", actor="agent")
        resume_scope = {"action": "resume", "target_mode": "READY"}
        controller.resume(
            approval(
                "resume",
                resume_scope,
                "control.local.write",
                approval_id="overrun-resume",
            ),
            idempotency_key="overrun-resume-operation",
            actor="agent",
        )
        result = controller.dispatch_run(
            job_id="job-overrun-live",
            idempotency_key="overrun-dispatch",
            actor="agent",
        )
        self.assertEqual(result["state"], "needs_review")
        self.assertTrue(result["overrun_detected"])
        self.assertEqual(result["stop_reason"], "USAGE_ESTIMATE_OVERRUN")
        self.assertEqual(controller.queue_get("job-overrun-live")["state"], "needs_review")
        self.assertEqual(controller.status()["controller"]["mode"], "PAUSED")

    def test_reconcile_moves_stale_work_run_and_job_to_review(self) -> None:
        clock = FixedClock()
        controller = Controller(config(":memory:"), Store(":memory:"), clock=clock)
        package = work_package("job-stale-work")
        self._approve_package(controller, package, "stale-work")
        with controller.store.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET state = 'running' WHERE job_id = 'job-stale-work'"
            )
            connection.execute(
                """INSERT INTO runs(
                       run_id, job_id, kind, state, thread_id, turn_id, started_at,
                       updated_at, lease_expires_at
                   ) VALUES ('run-stale-work', 'job-stale-work', 'work', 'running',
                             'thread-stale', 'turn-stale', ?, ?, ?)""",
                (NOW - 120, NOW - 120, NOW - 60),
            )
        plan = controller.reconcile_plan()
        self.assertEqual([item["run_id"] for item in plan["run_candidates"]], ["run-stale-work"])
        scope = {"action": "reconcile", "lease_ids": [], "run_ids": ["run-stale-work"]}
        result = controller.reconcile(
            approval(
                "reconcile",
                scope,
                "reconcile.local",
                approval_id="stale-work-reconcile",
            ),
            idempotency_key="stale-work-reconcile-operation",
            actor="agent",
        )
        self.assertEqual(result["reconciled_run_ids"], ["run-stale-work"])
        self.assertEqual(controller.monitor_get("run-stale-work")["state"], "needs_review")
        self.assertEqual(controller.queue_get("job-stale-work")["state"], "needs_review")
        controller.monitor = CodexThreadMonitor(
            command=[sys.executable, str(FIXTURE), "complete"]
        )
        refreshed = controller.monitor_refresh(
            "run-stale-work",
            idempotency_key="stale-work-monitor",
            actor="agent",
        )
        self.assertEqual(refreshed["state"], "needs_review")
        self.assertEqual(
            controller.monitor_get("run-stale-work")["stop_reason"],
            "POST_RUN_REVIEW_REQUIRED",
        )

    def test_operator_stop_interrupts_active_work_and_remains_stopped(self) -> None:
        clock = FixedClock()
        values = []
        for index, five_used in enumerate((20.0, 20.5, 21.0)):
            value = snapshot(five_used=five_used)
            values.append(value)
        holder = {}

        class StopRunner:
            def run(self, *, safety_check, on_started, **_kwargs):
                self.assert_safe(safety_check())
                on_started("thread-stop", "turn-stop")
                holder["controller"].stop(
                    reason_code="OPERATOR_STOP",
                    idempotency_key="operator-stop-control",
                    actor="operator",
                )
                self.assert_safe(not safety_check())
                return {
                    "state": "interrupted",
                    "stop_reason": "SAFETY_CHECK_FAILED",
                    "thread_id": "thread-stop",
                    "turn_id": "turn-stop",
                }

            @staticmethod
            def assert_safe(value):
                if not value:
                    raise AssertionError("unexpected safety result")

        controller = Controller(
            config(":memory:"),
            Store(":memory:"),
            clock=clock,
            probe=SequenceProbe(values),
            work_runner=StopRunner(),
        )
        holder["controller"] = controller
        package = work_package("job-stop-live")
        self._approve_package(controller, package, "stop-live")
        controller.probe_quota(idempotency_key="stop-initial", actor="agent")
        resume_scope = {"action": "resume", "target_mode": "READY"}
        controller.resume(
            approval(
                "resume",
                resume_scope,
                "control.local.write",
                approval_id="stop-resume",
            ),
            idempotency_key="stop-resume-operation",
            actor="agent",
        )
        result = controller.dispatch_run(
            job_id="job-stop-live",
            idempotency_key="stop-dispatch",
            actor="agent",
        )
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(controller.status()["controller"]["mode"], "STOPPED")
        self.assertEqual(controller.queue_get("job-stop-live")["state"], "needs_review")


if __name__ == "__main__":
    unittest.main()
