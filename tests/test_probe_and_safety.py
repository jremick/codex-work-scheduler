import io
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from codex_work_scheduler.constants import APP_SERVER_OUTBOUND_ALLOWLIST, WORK_OUTBOUND_ALLOWLIST
from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.probe import (
    CodexAppServerProbe,
    normalize_account_result,
    normalize_rate_limit_result,
)
from codex_work_scheduler.service import Controller
from codex_work_scheduler.store import Store
from codex_work_scheduler.work_runner import CodexWorkRunner

from tests.helpers import FixedClock, NOW, FakeProbe, config, snapshot


def app_server_result(five_used=20, weekly_used=30, plan_type="pro"):
    return {
        "rateLimits": {
            "limitId": "codex",
            "planType": plan_type,
            "credits": {"balance": "100", "hasCredits": True, "unlimited": False},
            "spendControlReached": False,
            "primary": {
                "usedPercent": five_used,
                "windowDurationMins": 300,
                "resetsAt": NOW + 3600,
            },
            "secondary": {
                "usedPercent": weekly_used,
                "windowDurationMins": 10080,
                "resetsAt": NOW + 7 * 86400,
            },
            "rateLimitReachedType": None,
        }
    }


def account_identity(plan_type="pro"):
    return {
        "account_fingerprint": "a" * 64,
        "account_plan_type": plan_type,
        "account_type": "chatgpt",
    }


class NonClosingStringIO(io.StringIO):
    def close(self):
        self.flush()


class FakeProcess:
    def __init__(self, lines):
        self.stdin = NonClosingStringIO()
        self.stdout = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class ProbeAndSafetyTests(unittest.TestCase):
    def test_probe_has_fixed_command_and_exact_outbound_allowlist(self) -> None:
        captured = {}
        process = FakeProcess(
            [
                {"id": 1, "result": {"userAgent": "test"}},
                {
                    "id": 2,
                    "result": {
                        "account": {"type": "chatgpt", "email": "person@example.com", "planType": "pro"},
                        "requiresOpenaiAuth": True,
                    },
                },
                {"id": 3, "result": app_server_result()},
            ]
        )

        def popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return process

        probe = CodexAppServerProbe(timeout_seconds=1, popen_factory=popen, clock=time.time)
        value = probe.read(
            profile_key="test-profile",
            limit_id="codex",
            account_fingerprint_key="ab" * 32,
        )
        self.assertEqual(captured["command"], ["codex", "app-server", "--stdio"])
        self.assertIs(captured["kwargs"]["stderr"], subprocess.DEVNULL)
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        methods = [message["method"] for message in sent]
        self.assertEqual(
            methods,
            ["initialize", "initialized", "account/read", "account/rateLimits/read"],
        )
        self.assertEqual(set(methods), APP_SERVER_OUTBOUND_ALLOWLIST)
        self.assertEqual(value["five_hour"]["window_duration_minutes"], 300)
        self.assertEqual(value["weekly"]["window_duration_minutes"], 10080)
        self.assertEqual(value["credit_signal"], "present")
        self.assertEqual(value["paid_credit_state"], "available")
        self.assertEqual(len(value["account_fingerprint"]), 64)
        self.assertNotIn("person@example.com", json.dumps(value))
        self.assertTrue(process.terminated)

    def test_account_identity_requires_chatgpt_email_and_is_redacted(self) -> None:
        value = normalize_account_result(
            {
                "account": {"type": "chatgpt", "email": "Person@Example.com", "planType": "pro"},
                "requiresOpenaiAuth": True,
            },
            fingerprint_key="cd" * 32,
        )
        self.assertEqual(value["account_type"], "chatgpt")
        self.assertNotIn("email", value)
        self.assertEqual(len(value["account_fingerprint"]), 64)

        with self.assertRaises(SchedulerError) as caught:
            normalize_account_result(
                {"account": {"type": "chatgpt", "email": None, "planType": "pro"}},
                fingerprint_key="cd" * 32,
            )
        self.assertEqual(caught.exception.code, "ACCOUNT_IDENTITY_UNAVAILABLE")

    def test_normalizer_requires_both_exact_window_durations(self) -> None:
        value = app_server_result()
        value["rateLimits"]["secondary"]["windowDurationMins"] = 60
        with self.assertRaises(SchedulerError) as caught:
            normalize_rate_limit_result(
                value,
                observed_at=NOW,
                profile_key="test-profile",
                limit_id="codex",
                account=account_identity(),
            )
        self.assertEqual(caught.exception.code, "SIGNAL_MISSING")

    def test_auto_selects_the_only_bucket_with_both_required_windows(self) -> None:
        complete = app_server_result()["rateLimits"]
        value = {
            "rateLimits": {
                "limitId": "codex",
                "primary": complete["secondary"],
                "secondary": None,
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": complete["secondary"],
                    "secondary": None,
                },
                "opaque_bucket": dict(complete, limitId="opaque_bucket"),
            },
        }
        normalized = normalize_rate_limit_result(
            value,
            observed_at=NOW,
            profile_key="test-profile",
            limit_id="auto",
            account=account_identity(),
        )
        self.assertEqual(normalized["limit_id"], "opaque_bucket")

        value["rateLimitsByLimitId"]["another"] = dict(complete, limitId="another")
        with self.assertRaises(SchedulerError) as caught:
            normalize_rate_limit_result(
                value,
                observed_at=NOW,
                profile_key="test-profile",
                limit_id="auto",
                account=account_identity(),
            )
        self.assertEqual(caught.exception.code, "SIGNAL_AMBIGUOUS")

    def test_reset_is_inferred_only_from_new_reset_time_and_lower_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FixedClock()
            fake = FakeProbe(snapshot(five_used=60, weekly_used=50))
            database_path = str(Path(directory) / "state.sqlite")
            controller = Controller(
                config(database_path),
                Store(database_path),
                clock=clock,
                probe=fake,
            )
            controller.probe_quota(idempotency_key="probe-before", actor="agent")
            fake.value = snapshot(
                five_used=5,
                weekly_used=50,
                five_reset=NOW + 7200,
                weekly_reset=NOW + 7 * 86400,
            )
            result = controller.probe_quota(idempotency_key="probe-after", actor="agent")
            self.assertEqual(result["reset_windows"], ["five_hour"])

    def test_plan_change_for_same_profile_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FixedClock()
            fake = FakeProbe(snapshot(plan_type="pro"))
            database_path = str(Path(directory) / "state.sqlite")
            controller = Controller(
                config(database_path),
                Store(database_path),
                clock=clock,
                probe=fake,
            )
            controller.probe_quota(idempotency_key="probe-pro", actor="agent")
            fake.value = snapshot(plan_type="team", five_used=21)
            with self.assertRaises(SchedulerError) as caught:
                controller.probe_quota(idempotency_key="probe-team", actor="agent")
            self.assertEqual(caught.exception.code, "PROFILE_MISMATCH")

    def test_account_switch_with_same_plan_and_bucket_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = FixedClock()
            fake = FakeProbe(snapshot(account_fingerprint="1" * 64))
            database_path = str(Path(directory) / "state.sqlite")
            controller = Controller(
                config(database_path),
                Store(database_path),
                clock=clock,
                probe=fake,
            )
            controller.probe_quota(idempotency_key="probe-account-one", actor="agent")
            fake.value = snapshot(account_fingerprint="2" * 64, five_used=21)
            with self.assertRaises(SchedulerError) as caught:
                controller.probe_quota(idempotency_key="probe-account-two", actor="agent")
            self.assertEqual(caught.exception.code, "PROFILE_MISMATCH")

    def test_production_package_contains_only_guarded_execution_and_no_listener_path(self) -> None:
        package_dir = Path(__file__).resolve().parents[1] / "codex_work_scheduler"
        sources = {path.name: path.read_text(encoding="utf-8") for path in package_dir.glob("*.py")}
        combined = "\n".join(sources.values())
        for forbidden in (
            "codex exec",
            "http.server",
            "socket.listen",
            "launchctl",
            "account/rateLimitResetCredit/consume",
            "account/sendAddCreditsNudgeEmail",
        ):
            self.assertNotIn(forbidden, combined)
        subprocess_files = sorted(
            name for name, source in sources.items() if "import subprocess" in source
        )
        self.assertEqual(
            subprocess_files,
            ["live_test.py", "monitor.py", "probe.py", "thread_control.py", "work_runner.py"],
        )
        self.assertNotIn("turn/start", "\n".join(
            source
            for name, source in sources.items()
            if name not in {"constants.py", "live_test.py", "thread_control.py", "work_runner.py"}
        ))
        self.assertEqual(CodexAppServerProbe.COMMAND, ("codex", "app-server", "--stdio"))
        self.assertEqual(CodexWorkRunner.COMMAND, ("codex", "app-server", "--stdio"))
        self.assertEqual(
            WORK_OUTBOUND_ALLOWLIST,
            {
                "initialize",
                "initialized",
                "hooks/list",
                "app/installed",
                "mcpServerStatus/list",
                "experimentalFeature/list",
                "thread/start",
                "turn/start",
                "turn/interrupt",
            },
        )


if __name__ == "__main__":
    unittest.main()
