import json
import tempfile
import unittest
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from codex_work_scheduler import __version__
from codex_work_scheduler.cli import build_parser, main
from codex_work_scheduler.constants import APP_VERSION
from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.util import make_envelope
from codex_work_scheduler.validation import validate_config, validate_job, validate_policy
from codex_work_scheduler.probe import CodexAppServerProbe

from tests.helpers import config, job


class ContractTests(unittest.TestCase):
    def test_public_package_version_matches_app_server_version(self) -> None:
        self.assertEqual(__version__, APP_VERSION)

    def test_example_config_supports_documented_background_commands(self) -> None:
        path = Path(__file__).resolve().parents[1] / "scheduler.example.json"
        capabilities = set(json.loads(path.read_text(encoding="utf-8"))["capabilities"])
        self.assertTrue(
            {
                "background.read",
                "background.run",
                "launchd.render",
                "notification.local.write",
            }.issubset(capabilities)
        )

    def test_all_published_schemas_are_valid_json_with_unique_ids(self) -> None:
        schema_dir = Path(__file__).resolve().parents[1] / "schemas"
        identifiers = []
        for path in sorted(schema_dir.glob("*.schema.json")):
            with path.open("r", encoding="utf-8") as handle:
                schema = json.load(handle)
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertEqual(schema["type"], "object")
            identifiers.append(schema["$id"])
        self.assertGreaterEqual(len(identifiers), 8)
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_config_requires_mandatory_dry_run(self) -> None:
        value = config(":memory:")
        value["dry_run"] = False
        with self.assertRaises(SchedulerError) as caught:
            validate_config(value)
        self.assertEqual(caught.exception.code, "DRY_RUN_REQUIRED")

    def test_config_rejects_prohibited_capability(self) -> None:
        value = config(":memory:")
        value["capabilities"].append("codex.turn.start")
        with self.assertRaises(SchedulerError) as caught:
            validate_config(value)
        self.assertEqual(caught.exception.code, "CAPABILITY_DENIED")

    def test_legacy_v1_config_defaults_live_dispatch_to_disabled(self) -> None:
        value = config(":memory:")
        value.pop("dispatch")
        validated = validate_config(value)
        self.assertFalse(validated["dispatch"]["enabled"])
        self.assertTrue(validated["dispatch"]["require_paid_credits_unavailable"])
        self.assertEqual(validated["dispatch"]["credit_verification_mode"], "machine_only")

    def test_operator_attested_credit_mode_is_explicit(self) -> None:
        value = config(":memory:")
        value["dispatch"]["credit_verification_mode"] = (
            "operator_attested_subscription_only"
        )
        validated = validate_config(value)
        self.assertEqual(
            validated["dispatch"]["credit_verification_mode"],
            "operator_attested_subscription_only",
        )

        value["dispatch"]["credit_verification_mode"] = "trust_me"
        with self.assertRaises(SchedulerError) as caught:
            validate_config(value)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_dispatch_safety_flags_cannot_be_disabled(self) -> None:
        value = config(":memory:")
        value["dispatch"]["deny_hooks"] = False
        with self.assertRaises(SchedulerError) as caught:
            validate_config(value)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_config_rejects_database_parent_traversal(self) -> None:
        value = config("../outside.sqlite")
        with self.assertRaises(SchedulerError) as caught:
            validate_config(value)
        self.assertEqual(caught.exception.code, "PATH_DENIED")

    def test_contracts_reject_unknown_fields(self) -> None:
        value = job()
        value["prompt"] = "must never become an executable payload"
        with self.assertRaises(SchedulerError) as caught:
            validate_job(value)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(caught.exception.details["unknown"], ["prompt"])

    def test_policy_restricts_dry_run_concurrency_to_one(self) -> None:
        value = config(":memory:")["policy"]
        value["max_concurrency"] = 2
        with self.assertRaises(SchedulerError) as caught:
            validate_policy(value)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_result_envelope_is_stable(self) -> None:
        self.assertEqual(
            make_envelope("status", "req-1", result={"dry_run": True}),
            {
                "schema_version": "1",
                "command": "status",
                "ok": True,
                "request_id": "req-1",
                "result": {"dry_run": True},
                "error": None,
            },
        )

    def test_cli_always_returns_json_for_success_and_argument_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "scheduler.json"
            value = config("state.sqlite")
            config_path.write_text(json.dumps(value), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--config", str(config_path), "status"])
            envelope = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["command"], "status")

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--config", str(config_path), "queue"])
            envelope = json.loads(output.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"], "INVALID_ARGUMENT")

    def test_approval_prepare_uses_operator_neutral_default(self) -> None:
        args = build_parser().parse_args(
            [
                "--config",
                "scheduler.example.json",
                "approval",
                "prepare",
                "--action",
                "resume",
            ]
        )
        self.assertEqual(args.approver, "operator")

    def test_cli_service_status_is_read_only_and_disabled_once_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "scheduler.json"
            value = config("state.sqlite")
            config_path.write_text(json.dumps(value), encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["--config", str(config_path), "service", "status"]
                )
            envelope = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(envelope["ok"])
            self.assertFalse(envelope["result"]["configured"]["enabled"])
            self.assertFalse(envelope["result"]["lease"]["running"])
            self.assertFalse((Path(directory) / ".scheduler" / "notifications.jsonl").exists())

            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(
                    ["--config", str(config_path), "service", "once"]
                )
            envelope = json.loads(output.getvalue())
            self.assertEqual(exit_code, 2)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"], "BACKGROUND_DISABLED")

    def test_cli_probe_and_cycle_use_fake_app_server_and_redact_identity(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "fake_app_server.py"
        original_command = CodexAppServerProbe.COMMAND
        try:
            CodexAppServerProbe.COMMAND = (sys.executable, str(fixture))
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "scheduler.json"
                config_path.write_text(json.dumps(config("state.sqlite")), encoding="utf-8")
                output = StringIO()
                with redirect_stdout(output):
                    exit_code = main(
                        [
                            "--config",
                            str(config_path),
                            "probe",
                            "--idempotency-key",
                            "fake-app-server-probe",
                        ]
                    )
                serialized = output.getvalue()
                envelope = json.loads(serialized)
                self.assertEqual(exit_code, 0)
                self.assertTrue(envelope["ok"])
                self.assertEqual(
                    envelope["result"]["outbound_methods"],
                    ["initialize", "initialized", "account/read", "account/rateLimits/read"],
                )
                self.assertNotIn("fixture-account@example.invalid", serialized)
                self.assertEqual(
                    len(envelope["result"]["snapshot"]["account_fingerprint"]), 64
                )

                output = StringIO()
                with redirect_stdout(output):
                    exit_code = main(
                        [
                            "--config",
                            str(config_path),
                            "cycle",
                            "--idempotency-key",
                            "fake-app-server-cycle",
                        ]
                    )
                serialized = output.getvalue()
                envelope = json.loads(serialized)
                self.assertEqual(exit_code, 0)
                self.assertEqual(
                    envelope["result"]["outcome"], "skipped_controller_not_ready"
                )
                self.assertIsNone(envelope["result"]["tick"])
                self.assertNotIn("fixture-account@example.invalid", serialized)
        finally:
            CodexAppServerProbe.COMMAND = original_command


if __name__ == "__main__":
    unittest.main()
