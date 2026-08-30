import json
import tempfile
import unittest
from pathlib import Path

from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.service import Controller
from codex_work_scheduler.store import Store
from codex_work_scheduler.util import payload_hash
from codex_work_scheduler.validation import validate_approval, validate_job

from tests.helpers import FixedClock, NOW, FakeProbe, approval, config, job, policy, snapshot


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = str(Path(self.temporary_directory.name) / "state.sqlite")
        self.clock = FixedClock()
        self.fake_probe = FakeProbe(snapshot())
        self.store = Store(self.database_path)
        self.controller = Controller(
            config(self.database_path),
            self.store,
            clock=self.clock,
            probe=self.fake_probe,
        )

    def observe_quota(self, key: str = "probe-1") -> dict:
        return self.controller.probe_quota(idempotency_key=key, actor="agent")

    def resume(self, approval_id: str = "approval-resume-1") -> dict:
        scoped = {"action": "resume", "target_mode": "READY"}
        grant = approval(
            "resume",
            scoped,
            "control.local.write",
            approval_id=approval_id,
            now=self.clock(),
        )
        return self.controller.resume(grant, idempotency_key="resume-%s" % approval_id, actor="agent")

    def submit(self, value: dict, approval_id: str = "approval-job-1", key: str = "submit-1") -> dict:
        grant = approval(
            "queue.submit",
            validate_job(value),
            "queue.local.write",
            approval_id=approval_id,
            now=self.clock(),
        )
        return self.controller.queue_submit(value, grant, idempotency_key=key, actor="agent")

    def make_ready(self) -> None:
        self.observe_quota()
        self.resume()

    def test_controller_starts_paused_and_missing_signal_blocks_preflight(self) -> None:
        status = self.controller.status()
        self.assertTrue(status["dry_run"])
        self.assertEqual(status["controller"]["mode"], "PAUSED")
        preflight = self.controller.preflight()
        self.assertFalse(preflight["safe_to_resume"])
        self.assertIn("SIGNAL_MISSING", preflight["issues"])

    def test_legacy_unbound_snapshot_fails_closed_until_phase_b_probe(self) -> None:
        legacy = snapshot()
        legacy["schema_version"] = "1"
        legacy.pop("account_fingerprint")
        legacy.pop("account_type")
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO quota_snapshots(
                       snapshot_id, observed_at, profile_key, limit_id, plan_type,
                       snapshot_json, snapshot_hash, created_at
                   ) VALUES ('legacy', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    legacy["observed_at"],
                    legacy["profile_key"],
                    legacy["limit_id"],
                    legacy["plan_type"],
                    json.dumps(legacy, sort_keys=True, separators=(",", ":")),
                    payload_hash(legacy),
                    self.clock(),
                ),
            )
        status = self.controller.status()
        self.assertFalse(status["quota"]["decision"]["eligible"])
        self.assertEqual(
            status["quota"]["decision"]["reasons"],
            ["ACCOUNT_IDENTITY_UNAVAILABLE"],
        )

    def test_approval_prepare_returns_an_unprivileged_machine_template(self) -> None:
        value = job()
        result = self.controller.approval_prepare(
            action="queue.submit",
            input_value=value,
            requested_approver="operator",
            suggested_ttl_seconds=900,
        )
        request = result["approval_request"]
        self.assertEqual(request["scope_hash"], payload_hash(validate_job(value)))
        self.assertEqual(request["capability"], "queue.local.write")
        self.assertFalse(result["template_is_approval"])
        with self.assertRaises(SchedulerError):
            validate_approval(result["approval_template"], now=self.clock())

    def test_probe_is_read_only_idempotent_and_stores_normalized_snapshot(self) -> None:
        first = self.observe_quota()
        replay = self.observe_quota()
        self.assertEqual(self.fake_probe.calls, 1)
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(
            first["outbound_methods"],
            ["initialize", "initialized", "account/read", "account/rateLimits/read"],
        )
        self.assertTrue(first["account_binding"]["bound"])
        self.assertEqual(len(first["account_binding"]["fingerprint_prefix"]), 12)
        self.assertEqual(self.store.latest_snapshot()["five_hour"]["window_duration_minutes"], 300)

    def test_resume_requires_fresh_signal_and_exact_approval(self) -> None:
        scoped = {"action": "resume", "target_mode": "READY"}
        grant = approval(
            "resume",
            scoped,
            "control.local.write",
            approval_id="approval-resume-missing-signal",
        )
        with self.assertRaises(SchedulerError) as caught:
            self.controller.resume(grant, idempotency_key="resume-missing", actor="agent")
        self.assertEqual(caught.exception.code, "SIGNAL_MISSING")

        self.observe_quota()
        wrong = approval(
            "resume",
            {"action": "resume", "target_mode": "PAUSED"},
            "control.local.write",
            approval_id="approval-resume-wrong",
        )
        with self.assertRaises(SchedulerError) as caught:
            self.controller.resume(wrong, idempotency_key="resume-wrong", actor="agent")
        self.assertEqual(caught.exception.code, "APPROVAL_SCOPE_MISMATCH")

        result = self.resume()
        self.assertEqual(result["mode"], "READY")

    def test_queue_approval_and_idempotency_are_enforced(self) -> None:
        value = job()
        first = self.submit(value)
        replay = self.submit(value)
        self.assertEqual(first["job_id"], "job-1")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(self.controller.queue_list()["jobs"]), 1)

        self.clock.advance(601)
        original_grant = approval(
            "queue.submit",
            validate_job(value),
            "queue.local.write",
            approval_id="approval-job-1",
            now=NOW,
        )
        expired_approval_replay = self.controller.queue_submit(
            value,
            original_grant,
            idempotency_key="submit-1",
            actor="agent",
        )
        self.assertTrue(expired_approval_replay["idempotent_replay"])

        other = job("job-2")
        other_grant = approval(
            "queue.submit",
            other,
            "queue.local.write",
            approval_id="approval-job-2",
        )
        with self.assertRaises(SchedulerError) as caught:
            self.controller.queue_submit(other, other_grant, idempotency_key="submit-1", actor="agent")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_tick_runs_only_fake_simulation_and_completes_lease(self) -> None:
        self.make_ready()
        self.submit(job())
        result = self.controller.tick(idempotency_key="tick-1", actor="agent")
        self.assertTrue(result["simulated"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["runner_result"]["runner"], "phase_b_fake_runner")
        self.assertEqual(result["state"], "simulated")
        self.assertEqual(self.controller.queue_get("job-1")["state"], "simulated")
        replay = self.controller.tick(idempotency_key="tick-1", actor="agent")
        self.assertTrue(replay["idempotent_replay"])

    def test_cycle_observes_then_skips_while_paused_and_is_idempotent(self) -> None:
        first = self.controller.cycle(idempotency_key="cycle-paused", actor="agent")
        replay = self.controller.cycle(idempotency_key="cycle-paused", actor="agent")
        self.assertEqual(first["outcome"], "skipped_controller_not_ready")
        self.assertIsNone(first["tick"])
        self.assertEqual(self.fake_probe.calls, 1)
        self.assertTrue(replay["idempotent_replay"])

    def test_cycle_runs_exactly_one_fake_job_when_ready(self) -> None:
        self.make_ready()
        self.submit(job())
        self.clock.advance(1)
        self.fake_probe.value = snapshot(observed_at=self.clock())
        result = self.controller.cycle(idempotency_key="cycle-ready", actor="agent")
        self.assertEqual(result["outcome"], "simulated")
        self.assertTrue(result["tick"]["simulated"])
        self.assertEqual(self.controller.queue_get("job-1")["state"], "simulated")
        replay = self.controller.cycle(idempotency_key="cycle-ready", actor="agent")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(self.fake_probe.calls, 2)

    def test_priority_and_reserve_policy_select_the_highest_eligible_job(self) -> None:
        self.make_ready()
        self.submit(
            job("job-too-large", priority=100, five_expected=60),
            approval_id="approval-too-large",
            key="submit-too-large",
        )
        self.submit(
            job("job-safe", priority=50, five_expected=5),
            approval_id="approval-safe",
            key="submit-safe",
        )
        result = self.controller.tick(idempotency_key="tick-priority", actor="agent")
        self.assertEqual(result["job_id"], "job-safe")
        self.assertEqual(self.controller.queue_get("job-too-large")["state"], "held_policy")

    def test_overrun_pauses_controller_and_requires_review(self) -> None:
        self.make_ready()
        overrun_job = job("job-overrun", five_expected=5, actual_five=8, actual_weekly=2)
        self.submit(overrun_job, approval_id="approval-overrun", key="submit-overrun")
        result = self.controller.tick(idempotency_key="tick-overrun", actor="agent")
        self.assertTrue(result["overrun"])
        self.assertTrue(result["requires_review"])
        self.assertEqual(self.controller.status()["controller"]["mode"], "PAUSED")
        self.assertEqual(self.controller.queue_get("job-overrun")["state"], "needs_review")

    def test_fake_failure_blocks_and_never_retries(self) -> None:
        self.make_ready()
        failing = job("job-failure", outcome="failure", actual_five=5, actual_weekly=1)
        self.submit(failing, approval_id="approval-failure", key="submit-failure")
        result = self.controller.tick(idempotency_key="tick-failure", actor="agent")
        self.assertFalse(result["simulated"])
        self.assertEqual(result["failure_code"], "SIMULATION_FAILED")
        self.assertEqual(self.controller.status()["controller"]["mode"], "BLOCKED")
        self.assertEqual(self.controller.queue_get("job-failure")["state"], "needs_review")

    def test_process_crash_leaves_a_durable_lease_for_reconciliation(self) -> None:
        class CrashRunner:
            def run(self, _job):
                raise RuntimeError("simulated process crash")

        self.make_ready()
        self.submit(job("job-process-crash"), approval_id="approval-process-crash", key="submit-process-crash")
        self.controller.runner = CrashRunner()
        with self.assertRaises(RuntimeError):
            self.controller.tick(idempotency_key="tick-process-crash", actor="agent")
        self.assertEqual(self.controller.queue_get("job-process-crash")["state"], "simulating")
        self.assertEqual(self.controller.status()["counts"]["active_leases"], 1)
        self.clock.advance(61)
        plan = self.controller.reconcile_plan()
        self.assertEqual(len(plan["candidates"]), 1)
        self.assertEqual(plan["candidates"][0]["job_id"], "job-process-crash")

    def test_pause_and_stop_are_immediate_but_resume_needs_approval(self) -> None:
        self.make_ready()
        paused = self.controller.pause(
            reason_code="OPERATOR_REQUEST",
            idempotency_key="pause-1",
            actor="agent",
        )
        self.assertEqual(paused["mode"], "PAUSED")
        stopped = self.controller.stop(
            reason_code="OPERATOR_REQUEST",
            idempotency_key="stop-1",
            actor="agent",
        )
        self.assertEqual(stopped["mode"], "STOPPED")
        resumed = self.resume("approval-resume-2")
        self.assertEqual(resumed["mode"], "READY")

    def test_policy_change_pauses_controller_until_new_preflight(self) -> None:
        self.make_ready()
        changed_policy = policy(five_hour_reserve_percent=15.0)
        grant = approval(
            "policy.set",
            changed_policy,
            "policy.local.write",
            approval_id="approval-policy-change",
        )
        result = self.controller.policy_set(
            changed_policy,
            grant,
            idempotency_key="policy-change-1",
            actor="agent",
        )
        self.assertEqual(result["version"], 2)
        state = self.controller.status()["controller"]
        self.assertEqual(state["mode"], "PAUSED")
        self.assertEqual(state["reason_code"], "POLICY_CHANGED")

    def test_resume_idempotency_replays_after_approval_and_signal_expire(self) -> None:
        self.observe_quota()
        scoped = {"action": "resume", "target_mode": "READY"}
        grant = approval(
            "resume",
            scoped,
            "control.local.write",
            approval_id="approval-resume-expiring",
            now=self.clock(),
        )
        first = self.controller.resume(grant, idempotency_key="resume-expiring", actor="agent")
        self.clock.advance(601)
        replay = self.controller.resume(grant, idempotency_key="resume-expiring", actor="agent")
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])

    def test_expired_lease_reconciliation_moves_job_to_manual_review(self) -> None:
        self.make_ready()
        self.submit(job("job-crashed"), approval_id="approval-crashed", key="submit-crashed")
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET state = 'simulating' WHERE job_id = 'job-crashed'"
            )
            connection.execute(
                """INSERT INTO leases(lease_id, job_id, owner, state, acquired_at, expires_at)
                   VALUES ('lease-crashed', 'job-crashed', 'agent', 'active', ?, ?)""",
                (NOW - 120, NOW - 60),
            )
        plan = self.controller.reconcile_plan()
        self.assertEqual([item["lease_id"] for item in plan["candidates"]], ["lease-crashed"])
        scoped = {"action": "reconcile", "lease_ids": ["lease-crashed"], "run_ids": []}
        grant = approval(
            "reconcile",
            scoped,
            "reconcile.local",
            approval_id="approval-reconcile",
        )
        result = self.controller.reconcile(grant, idempotency_key="reconcile-1", actor="agent")
        self.assertEqual(result["reconciled_lease_ids"], ["lease-crashed"])
        self.assertEqual(result["controller_mode"], "PAUSED")
        self.assertEqual(self.controller.queue_get("job-crashed")["state"], "needs_review")
        replay = self.controller.reconcile(grant, idempotency_key="reconcile-1", actor="agent")
        self.assertTrue(replay["idempotent_replay"])

    def test_capability_denial_is_fail_closed(self) -> None:
        limited = config(self.database_path, capabilities=["control.read"])
        other_store = Store(str(Path(self.temporary_directory.name) / "limited.sqlite"))
        controller = Controller(limited, other_store, clock=self.clock, probe=self.fake_probe)
        with self.assertRaises(SchedulerError) as caught:
            controller.queue_list()
        self.assertEqual(caught.exception.code, "CAPABILITY_DENIED")

    def test_audit_chain_detects_tampering_and_contains_no_job_payload(self) -> None:
        self.submit(job())
        events = self.controller.audit_list(limit=10)["events"]
        self.assertEqual(events[0]["event_type"], "queue.submitted")
        serialized = json.dumps(events)
        self.assertNotIn("work-job-1", serialized)
        self.assertNotIn("expected_usage", serialized)
        self.assertTrue(self.controller.audit_verify()["valid"])
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE audit_events SET details_json = '{\"tampered\":true}' WHERE sequence = 1"
            )
        verification = self.controller.audit_verify()
        self.assertFalse(verification["valid"])
        self.assertEqual(verification["first_invalid_sequence"], 1)


if __name__ == "__main__":
    unittest.main()
