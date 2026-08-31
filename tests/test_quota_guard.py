import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.quota_guard import (
    QuotaGuardCoordinator,
    can_resume,
    decide_resume,
    decide_trip,
)
from codex_work_scheduler.store import Store
from codex_work_scheduler.util import payload_hash

from tests.helpers import FixedClock, NOW, policy, snapshot


def _seed_approval(store: Store, approval_id: str = "guard-approval") -> None:
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO approvals(
                   approval_id, action, actor, scope_hash, capabilities_json,
                   granted_at, expires_at, approval_hash
               ) VALUES (?, 'quota_guard.arm', 'operator', ?, '[]', ?, ?, ?)""",
            (approval_id, "a" * 64, NOW - 1, NOW + 600, "b" * 64),
        )


def _record_snapshot(store: Store, value: dict, snapshot_id: str) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO quota_snapshots(
                   snapshot_id, observed_at, profile_key, limit_id, plan_type,
                   snapshot_json, snapshot_hash, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                value["observed_at"],
                value["profile_key"],
                value["limit_id"],
                value["plan_type"],
                encoded,
                payload_hash(value),
                value["observed_at"],
            ),
        )


class FakeAdapter:
    def __init__(self, *, targets=None) -> None:
        self.targets = {}
        self.goals = {}
        self.events = []
        for thread_id, state in (targets or {}).items():
            self.targets[thread_id] = copy.deepcopy(state)
            self.goals[thread_id] = (
                {"status": state.get("goal", "none"), "updated_at": 1}
                if state.get("goal")
                else None
            )

    def inventory(self):
        self.events.append(("inventory",))
        return list(self.targets)

    def read_thread(self, thread_id, include_turns=True):
        self.events.append(("read", thread_id, include_turns))
        value = copy.deepcopy(self.targets[thread_id])
        value["id"] = thread_id
        return {"thread": value}

    def get_goal(self, thread_id):
        self.events.append(("goal", thread_id))
        return copy.deepcopy(self.goals.get(thread_id))

    def set_goal_status(self, thread_id, status):
        self.events.append(("set_goal", thread_id, status))
        if self.goals.get(thread_id) is None:
            self.goals[thread_id] = {"status": status, "updated_at": 1}
        else:
            self.goals[thread_id]["status"] = status
            self.goals[thread_id]["updated_at"] += 1

    def interrupt(self, thread_id, turn_id):
        self.events.append(("interrupt", thread_id, turn_id))
        turns = self.targets[thread_id].setdefault("turns", [])
        for turn in turns:
            if turn.get("id") == turn_id:
                turn["status"] = "interrupted"
        self.targets[thread_id]["status"] = "idle"

    def reopen_thread(self, thread_id):
        self.events.append(("reopen", thread_id))
        self.targets[thread_id]["status"] = "idle"

    def start_fixed_continuation(self, thread_id):
        self.events.append(("continuation", thread_id))
        self.targets[thread_id]["status"] = "active"
        turn_id = "continuation-%s" % thread_id
        self.targets[thread_id]["turns"] = [{"id": turn_id, "status": "inProgress"}]
        return {"thread_id": thread_id, "turn_id": turn_id, "accepted": True}


class ReplacementTurnAdapter(FakeAdapter):
    def interrupt(self, thread_id, turn_id):
        super().interrupt(thread_id, turn_id)
        self.targets[thread_id]["status"] = "active"
        self.targets[thread_id]["turns"].append(
            {"id": "replacement-%s" % thread_id, "status": "inProgress"}
        )


class GoalReadRaceAdapter(FakeAdapter):
    def __init__(self, **values):
        super().__init__(**values)
        self.goal_reads = 0
        self.on_fourth_goal_read = None

    def get_goal(self, thread_id):
        self.goal_reads += 1
        if self.goal_reads == 4 and self.on_fourth_goal_read is not None:
            self.on_fourth_goal_read(thread_id)
        return super().get_goal(thread_id)


def _active(thread_id: str = "thread-1", *, goal: str = "active") -> dict:
    return {
        "status": "active",
        "goal": goal,
        "turns": [{"id": "turn-%s" % thread_id, "status": "inProgress"}],
    }


class QuotaGuardDomainTests(unittest.TestCase):
    def test_trip_is_triggered_by_either_fresh_window(self) -> None:
        five_low = snapshot(five_used=95, weekly_used=20)
        weekly_low = snapshot(five_used=20, weekly_used=95)
        self.assertEqual(decide_trip(five_low, 10, now=NOW)["tripped_windows"], ["five_hour"])
        self.assertEqual(decide_trip(weekly_low, 10, now=NOW)["tripped_windows"], ["weekly"])
        self.assertFalse(decide_trip(snapshot(five_used=20, weekly_used=30), 10, now=NOW)["trip"])

    def test_resume_requires_hysteresis_and_both_reset_proofs(self) -> None:
        stopped = snapshot(five_used=95, weekly_used=20)
        current = snapshot(
            observed_at=NOW + 10,
            five_used=5,
            weekly_used=20,
            five_reset=NOW + 7200,
            weekly_reset=NOW + 7 * 86400 + 1,
        )
        self.assertTrue(
            can_resume(
                current,
                stopped,
                10,
                5,
                tripped_windows=["five_hour"],
                now=NOW + 10,
            )
        )
        no_reset = copy.deepcopy(current)
        no_reset["five_hour"]["resets_at"] = stopped["five_hour"]["resets_at"]
        decision = decide_resume(
            no_reset,
            stopped,
            10,
            5,
            tripped_windows=["five_hour"],
            now=NOW + 10,
        )
        self.assertFalse(decision["safe_to_resume"])
        self.assertIn("RESET_NOT_CONFIRMED", decision["reasons"])

    def test_missing_or_stale_signal_fails_closed(self) -> None:
        self.assertTrue(decide_trip(None, 10, now=NOW)["contain"])
        stale = snapshot(observed_at=NOW - 301)
        decision = decide_trip(stale, 10, now=NOW)
        self.assertTrue(decision["contain"])
        self.assertEqual(decision["signal_status"], "stale")
        self.assertFalse(
            decide_resume(stale, snapshot(five_used=95), 10, 5, now=NOW)["safe_to_resume"]
        )

    def test_v1_db_migrates_and_future_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite")
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO metadata VALUES ('schema_version', '1')")
            connection.commit()
            connection.close()
            store = Store(path)
            with store.reader() as connection:
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(version, "2")
            self.assertIn("quota_guard_sessions", tables)
            self.assertIn("quota_guard_targets", tables)

            with store.transaction() as connection:
                connection.execute(
                    "UPDATE metadata SET value = '3' WHERE key = 'schema_version'"
                )
            with self.assertRaises(SchedulerError) as caught:
                Store(path)
            self.assertEqual(caught.exception.code, "SCHEMA_UNSUPPORTED")

    def _coordinator(self, adapter: FakeAdapter, *, resume_non_goal_threads=False):
        store = Store(":memory:")
        store.bootstrap(policy=policy(), now=NOW)
        _seed_approval(store)
        plan = {
            "profile_key": "test-profile",
            "limit_id": "codex",
            "threshold_remaining_percent": 10,
            "resume_hysteresis_percent": 5,
            "check_interval_seconds": 1,
            "resume_non_goal_threads": resume_non_goal_threads,
            "targets": list(adapter.targets),
        }
        return store, QuotaGuardCoordinator(store, adapter, clock=FixedClock()), plan

    def test_pause_before_interrupt_and_goal_ownership_restore(self) -> None:
        adapter = FakeAdapter(targets={"thread-1": _active()})
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-1")
        _record_snapshot(store, snapshot(five_used=95), "low")
        result = coordinator.one_due_cycle(now=NOW)
        self.assertEqual(result["outcomes"][0]["state"], "HELD_QUOTA")
        names = [event[0] for event in adapter.events]
        self.assertLess(names.index("set_goal"), names.index("interrupt"))
        self.assertEqual(store.get_quota_guard_target("guard-1", "thread-1")["state"], "HELD")
        self.assertTrue(store.get_quota_guard_target("guard-1", "thread-1")["goal_changed_by_guard"])

        reset = snapshot(
            observed_at=NOW + 2,
            five_used=5,
            five_reset=NOW + 7200,
            weekly_reset=NOW + 7 * 86400 + 1,
        )
        _record_snapshot(store, reset, "reset")
        coordinator.one_due_cycle(now=NOW + 2)
        self.assertEqual(adapter.goals["thread-1"]["status"], "active")
        self.assertEqual(store.get_quota_guard_target("guard-1", "thread-1")["state"], "RESUMED")

    def test_completed_target_is_not_restarted(self) -> None:
        adapter = FakeAdapter(
            targets={
                "thread-1": {
                    "status": "idle",
                    "turns": [{"id": "turn-thread-1", "status": "completed"}],
                }
            }
        )
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-completed")
        _record_snapshot(store, snapshot(five_used=95), "low-completed")
        coordinator.one_due_cycle(now=NOW)
        self.assertEqual(
            store.get_quota_guard_target("guard-completed", "thread-1")["state"], "COMPLETED"
        )
        self.assertNotIn("interrupt", [event[0] for event in adapter.events])

    def test_disarm_is_idempotent_and_does_not_resume(self) -> None:
        adapter = FakeAdapter(targets={"thread-1": _active()})
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-disarm")
        first = coordinator.disarm("guard-disarm", now=NOW)
        second = coordinator.disarm("guard-disarm", now=NOW + 1)
        self.assertEqual(first["state"], "DISARMED")
        self.assertEqual(second["state"], "DISARMED")
        self.assertNotIn("set_goal", [event[0] for event in adapter.events])

    def test_non_goal_resume_requires_explicit_opt_in(self) -> None:
        adapter = FakeAdapter(targets={"thread-1": _active(goal="none")})
        store, coordinator, plan = self._coordinator(adapter, resume_non_goal_threads=True)
        coordinator.create(plan, "guard-approval", guard_id="guard-nongoal")
        _record_snapshot(store, snapshot(five_used=95), "low-nongoal")
        coordinator.one_due_cycle(now=NOW)
        reset = snapshot(
            observed_at=NOW + 2,
            five_used=5,
            five_reset=NOW + 7200,
            weekly_reset=NOW + 7 * 86400 + 1,
        )
        _record_snapshot(store, reset, "reset-nongoal")
        coordinator.one_due_cycle(now=NOW + 2)
        names = [event[0] for event in adapter.events]
        self.assertIn("reopen", names)
        self.assertIn("continuation", names)

    def test_active_thread_without_exact_turn_fails_closed(self) -> None:
        adapter = FakeAdapter(
            targets={"thread-1": {"status": "active", "turns": [], "goal": "none"}}
        )
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-no-turn")
        _record_snapshot(store, snapshot(five_used=95), "low-no-turn")
        result = coordinator.one_due_cycle(now=NOW)
        self.assertEqual(result["outcomes"][0]["state"], "NEEDS_REVIEW")
        self.assertEqual(
            store.get_quota_guard_target("guard-no-turn", "thread-1")["reason_code"],
            "TURN_MISSING",
        )

    def test_replacement_turn_after_interrupt_fails_closed(self) -> None:
        adapter = ReplacementTurnAdapter(targets={"thread-1": _active(goal="none")})
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-replacement")
        _record_snapshot(store, snapshot(five_used=95), "low-replacement")
        result = coordinator.one_due_cycle(now=NOW)
        self.assertEqual(result["outcomes"][0]["state"], "NEEDS_REVIEW")
        self.assertEqual(
            store.get_quota_guard_target("guard-replacement", "thread-1")["reason_code"],
            "REPLACEMENT_TURN_ACTIVE",
        )

    def test_goal_ownership_token_prevents_manual_pause_override(self) -> None:
        adapter = FakeAdapter(targets={"thread-1": _active()})
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-ownership")
        _record_snapshot(store, snapshot(five_used=95), "low-ownership")
        coordinator.one_due_cycle(now=NOW)
        adapter.goals["thread-1"]["updated_at"] += 1
        reset = snapshot(
            observed_at=NOW + 2,
            five_used=5,
            five_reset=NOW + 7200,
            weekly_reset=NOW + 7 * 86400 + 1,
        )
        _record_snapshot(store, reset, "reset-ownership")
        result = coordinator.one_due_cycle(now=NOW + 2)
        self.assertEqual(result["outcomes"][0]["state"], "NEEDS_REVIEW")
        self.assertEqual(adapter.goals["thread-1"]["status"], "paused")

    def test_manual_goal_restart_is_not_reclaimed_by_guard(self) -> None:
        adapter = FakeAdapter(targets={"thread-1": _active()})
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-manual-goal")
        _record_snapshot(store, snapshot(five_used=95), "low-manual-goal")
        coordinator.one_due_cycle(now=NOW)
        adapter.goals["thread-1"].update({"status": "active", "updated_at": 99})
        adapter.targets["thread-1"] = _active()
        reset = snapshot(
            observed_at=NOW + 2,
            five_used=5,
            five_reset=NOW + 7200,
            weekly_reset=NOW + 7 * 86400 + 1,
        )
        _record_snapshot(store, reset, "reset-manual-goal")
        result = coordinator.one_due_cycle(now=NOW + 2)
        self.assertEqual(result["outcomes"][0]["state"], "NEEDS_REVIEW")
        self.assertEqual(adapter.goals["thread-1"]["status"], "active")
        self.assertEqual(
            [event for event in adapter.events if event[:2] == ("set_goal", "thread-1")][-1],
            ("set_goal", "thread-1", "paused"),
        )

    def test_turn_or_controller_race_blocks_goal_resume(self) -> None:
        for race in ("turn", "controller"):
            with self.subTest(race=race):
                adapter = GoalReadRaceAdapter(targets={"thread-1": _active()})
                store, coordinator, plan = self._coordinator(adapter)
                coordinator.create(plan, "guard-approval", guard_id="guard-race-%s" % race)
                _record_snapshot(store, snapshot(five_used=95), "low-race-%s" % race)
                coordinator.one_due_cycle(now=NOW)

                def inject(thread_id):
                    if race == "turn":
                        adapter.targets[thread_id] = _active(thread_id)
                    else:
                        with store.transaction() as connection:
                            store.set_controller(
                                connection,
                                mode="STOPPED",
                                reason_code="TEST_STOPPED_RACE",
                                now=NOW + 2,
                            )

                adapter.on_fourth_goal_read = inject
                reset = snapshot(
                    observed_at=NOW + 2,
                    five_used=5,
                    five_reset=NOW + 7200,
                    weekly_reset=NOW + 7 * 86400 + 1,
                )
                _record_snapshot(store, reset, "reset-race-%s" % race)
                result = coordinator.one_due_cycle(now=NOW + 2)
                self.assertEqual(result["outcomes"][0]["state"], "NEEDS_REVIEW")
                self.assertEqual(adapter.goals["thread-1"]["status"], "paused")

    def test_paid_credit_signal_blocks_resume_after_reset(self) -> None:
        adapter = FakeAdapter(targets={"thread-1": _active()})
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-credit")
        _record_snapshot(store, snapshot(five_used=95), "low-credit")
        coordinator.one_due_cycle(now=NOW)
        reset = snapshot(
            observed_at=NOW + 2,
            five_used=5,
            five_reset=NOW + 7200,
            weekly_reset=NOW + 7 * 86400 + 1,
        )
        reset["paid_credit_state"] = "available"
        reset["credit_signal"] = "positive"
        _record_snapshot(store, reset, "reset-credit")
        result = coordinator.one_due_cycle(now=NOW + 2)
        self.assertEqual(result["outcomes"][0]["state"], "HELD_QUOTA")
        self.assertEqual(result["outcomes"][0]["reason_code"], "PAID_CREDIT_SIGNAL_UNSAFE")
        self.assertEqual(adapter.goals["thread-1"]["status"], "paused")

    def test_store_enforces_single_active_guard_and_validates_state_reads(self) -> None:
        adapter = FakeAdapter(targets={"thread-1": _active()})
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-one")
        with self.assertRaises(SchedulerError):
            coordinator.create(plan, "guard-approval", guard_id="guard-two")
        with store.transaction() as connection:
            connection.execute(
                "UPDATE quota_guard_sessions SET state = 'UNKNOWN' WHERE guard_id = 'guard-one'"
            )
        with self.assertRaises(SchedulerError) as caught:
            store.get_quota_guard_session("guard-one")
        self.assertEqual(caught.exception.code, "STATE_INVALID")

    def test_crash_recovery_fences_all_nonterminal_targets_in_pending_session(self) -> None:
        adapter = FakeAdapter(targets={"thread-1": _active()})
        store, coordinator, plan = self._coordinator(adapter)
        coordinator.create(plan, "guard-approval", guard_id="guard-crash")
        session = store.get_quota_guard_session("guard-crash")
        store.transition_quota_guard_session(
            "guard-crash",
            "STOPPING",
            expected_revision=session["revision"],
            expected_state="ARMED",
            now=NOW,
        )
        recovered = store.recover_quota_guard_pending(now=NOW + 1)
        self.assertEqual(recovered, ["guard-crash"])
        self.assertEqual(
            store.get_quota_guard_target("guard-crash", "thread-1")["state"],
            "NEEDS_REVIEW",
        )


if __name__ == "__main__":
    unittest.main()
