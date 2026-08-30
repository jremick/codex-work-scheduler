import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.thread_control import (
    FIXED_CONTINUATION_PROMPT,
    THREAD_CONTROL_OUTBOUND_ALLOWLIST,
    CodexThreadControl,
)


FIXTURE = Path(__file__).parent / "fixtures" / "fake_thread_control_server.py"
THREAD_ID = "thread-control-fixture"
ACTIVE_TURN_ID = "turn-control-active"


class NonClosingStringIO(io.StringIO):
    def close(self):
        self.flush()


class FakeProcess:
    def __init__(self, responses):
        self.stdin = NonClosingStringIO()
        self.stdout = io.StringIO("".join(json.dumps(value) + "\n" for value in responses))
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class ThreadControlTests(unittest.TestCase):
    def control(self, scenario="happy", *, timeout_seconds=1.0):
        return CodexThreadControl(
            command=[sys.executable, str(FIXTURE), scenario],
            timeout_seconds=timeout_seconds,
        )

    def test_allowlist_and_default_command_are_fixed(self):
        self.assertEqual(
            THREAD_CONTROL_OUTBOUND_ALLOWLIST,
            {
                "initialize",
                "initialized",
                "thread/list",
                "thread/read",
                "thread/goal/get",
                "thread/goal/set",
                "thread/resume",
                "turn/interrupt",
                "turn/start",
            },
        )
        self.assertEqual(CodexThreadControl.COMMAND, ("codex", "app-server", "proxy"))
        with self.assertRaises(SchedulerError) as caught:
            CodexThreadControl(command="codex app-server --stdio")
        self.assertEqual(caught.exception.code, "THREAD_CONTROL_COMMAND_INVALID")

    def test_injected_process_uses_shell_false_and_matches_ids(self):
        captured = {}
        process = FakeProcess(
            [
                {"id": 1, "result": {"userAgent": "fixture"}},
                {
                    "id": 2,
                    "result": {
                        "goal": {
                            "threadId": "thread-x",
                            "objective": "private objective",
                            "status": "paused",
                            "updatedAt": 11,
                        }
                    },
                },
            ]
        )

        def popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return process

        control = CodexThreadControl(popen_factory=popen, timeout_seconds=1.0)
        result = control.set_goal_status("thread-x", "paused")
        self.assertEqual(result, {"thread_id": "thread-x", "status": "paused", "updated_at": 11})
        self.assertEqual(captured["command"], ["codex", "app-server", "proxy"])
        self.assertFalse(captured["kwargs"]["shell"])
        self.assertIs(captured["kwargs"]["stderr"], subprocess.DEVNULL)
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual([message["method"] for message in sent], ["initialize", "initialized", "thread/goal/set"])
        self.assertEqual(sent[-1]["params"], {"threadId": "thread-x", "status": "paused"})
        self.assertNotIn("objective", json.dumps(sent[-1]))
        self.assertTrue(process.terminated)

    def test_inventory_follows_cursor_and_extracts_only_explicit_active_turn(self):
        result = self.control("pagination").inventory()
        self.assertEqual(
            result,
            [{"thread_id": THREAD_ID, "status": "active", "active_turn_id": ACTIVE_TURN_ID}],
        )

    def test_inventory_keeps_missing_active_turn_id_unknown(self):
        result = self.control("happy").inventory_active_threads()
        self.assertEqual(
            result,
            [{"thread_id": THREAD_ID, "status": "active", "active_turn_id": None}],
        )

    def test_read_includes_sanitized_turns_and_extracts_active_id(self):
        result = self.control("happy").read_thread(THREAD_ID)
        self.assertEqual(result["thread_id"], THREAD_ID)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["active_turn_id"], ACTIVE_TURN_ID)
        self.assertEqual(
            result["turns"],
            [
                {"turn_id": "turn-finished", "status": "completed"},
                {"turn_id": ACTIVE_TURN_ID, "status": "inProgress"},
            ],
        )
        serialized = json.dumps(result)
        self.assertNotIn("items", serialized)
        self.assertNotIn("must-not-leak-output", serialized)

    def test_goal_read_and_status_update_do_not_retain_objective_or_usage(self):
        control = self.control("happy")
        self.assertEqual(
            control.get_goal(THREAD_ID),
            {"thread_id": THREAD_ID, "status": "active", "updated_at": 10},
        )
        self.assertEqual(
            control.set_goal_status(THREAD_ID, "paused"),
            {"thread_id": THREAD_ID, "status": "paused", "updated_at": 11},
        )
        self.assertNotIn("must-not-leak-objective", json.dumps(control.get_goal(THREAD_ID)))

    def test_interrupt_sends_exact_turn_id(self):
        result = self.control("happy").interrupt_turn(THREAD_ID, ACTIVE_TURN_ID)
        self.assertEqual(
            result,
            {"thread_id": THREAD_ID, "turn_id": ACTIVE_TURN_ID, "accepted": True},
        )

    def test_resume_and_continuation_uses_fixed_prompt_on_existing_thread(self):
        result = self.control("happy").resume_and_continue(THREAD_ID)
        self.assertEqual(result["thread_id"], THREAD_ID)
        self.assertEqual(result["turn_id"], "turn-control-continuation")
        self.assertEqual(result["status"], "inProgress")
        self.assertEqual(FIXED_CONTINUATION_PROMPT.count("approved scope"), 1)
        self.assertNotIn("thread-control-fixture", FIXED_CONTINUATION_PROMPT)

    def test_malformed_and_ambiguous_responses_fail_closed(self):
        with self.assertRaises(SchedulerError) as malformed:
            self.control("malformed").inventory()
        self.assertEqual(malformed.exception.code, "THREAD_CONTROL_PROTOCOL_ERROR")
        with self.assertRaises(SchedulerError) as ambiguous:
            self.control("ambiguous").read_thread(THREAD_ID)
        self.assertEqual(ambiguous.exception.code, "THREAD_CONTROL_AMBIGUOUS")

    def test_denied_method_is_rejected_before_starting_process(self):
        control = CodexThreadControl()
        with self.assertRaises(SchedulerError) as caught:
            control.allow("account/read")
        self.assertEqual(caught.exception.code, "METHOD_DENIED")

    def test_timeout_and_server_rejection_have_stable_codes(self):
        with self.assertRaises(SchedulerError) as timeout:
            self.control("timeout", timeout_seconds=0.05).inventory()
        self.assertEqual(timeout.exception.code, "THREAD_CONTROL_TIMEOUT")
        self.assertFalse(timeout.exception.retryable)

        with self.assertRaises(SchedulerError) as rejected:
            self.control("reject").resume_thread(THREAD_ID)
        self.assertEqual(rejected.exception.code, "THREAD_CONTROL_REJECTED")
        self.assertFalse(rejected.exception.retryable)

    def test_mismatched_response_id_fails_closed(self):
        process = FakeProcess(
            [
                {"id": 1, "result": {"userAgent": "fixture"}},
                {"id": 99, "result": {"data": [], "nextCursor": None}},
            ]
        )
        control = CodexThreadControl(
            popen_factory=lambda command, **kwargs: process,
            timeout_seconds=1.0,
        )
        with self.assertRaises(SchedulerError) as caught:
            control.inventory()
        self.assertEqual(caught.exception.code, "THREAD_CONTROL_RESPONSE_MISMATCH")


if __name__ == "__main__":
    unittest.main()
