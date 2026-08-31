import copy
import json
import unittest
from pathlib import Path

from codex_work_scheduler.constants import (
    LOCAL_DRY_RUN_CAPABILITIES,
    QUOTA_GUARD_OUTBOUND_ALLOWLIST,
)
from codex_work_scheduler.errors import SchedulerError
from codex_work_scheduler.validation import (
    validate_config,
    validate_quota_guard_config,
    validate_quota_guard_plan,
)

from tests.helpers import config


QUOTA_GUARD_DEFAULTS = {
    "enabled": False,
    "min_check_interval_seconds": 60,
    "max_check_interval_seconds": 3600,
    "max_targets": 100,
    "resume_hysteresis_percent": 1.0,
}


def quota_guard_plan(**overrides):
    value = {
        "schema_version": "1",
        "threshold_remaining_percent": 20.0,
        "check_interval_seconds": 120,
        "target_thread_ids": ["thread-z", "thread-a"],
        "resume_non_goal_threads": False,
    }
    value.update(overrides)
    return value


class QuotaGuardContractTests(unittest.TestCase):
    def test_capabilities_and_outbound_allowlist_are_fixed(self):
        self.assertTrue(
            {
                "quota_guard.read",
                "quota_guard.local.write",
                "quota_guard.thread.control",
            }.issubset(LOCAL_DRY_RUN_CAPABILITIES)
        )
        self.assertEqual(
            QUOTA_GUARD_OUTBOUND_ALLOWLIST,
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

    def test_legacy_config_gets_disabled_quota_guard_defaults(self):
        value = config(":memory:")
        validated = validate_config(value)
        self.assertEqual(validated["quota_guard"], QUOTA_GUARD_DEFAULTS)

        value.pop("quota_guard", None)
        self.assertEqual(validate_config(value)["quota_guard"], QUOTA_GUARD_DEFAULTS)

    def test_quota_guard_config_is_strict_and_enforces_ranges(self):
        self.assertEqual(
            validate_quota_guard_config(QUOTA_GUARD_DEFAULTS), QUOTA_GUARD_DEFAULTS
        )
        invalid = copy.deepcopy(QUOTA_GUARD_DEFAULTS)
        invalid["max_check_interval_seconds"] = 59
        with self.assertRaises(SchedulerError):
            validate_quota_guard_config(invalid)

        invalid = copy.deepcopy(QUOTA_GUARD_DEFAULTS)
        invalid["min_check_interval_seconds"] = 3600
        invalid["max_check_interval_seconds"] = 3599
        with self.assertRaises(SchedulerError):
            validate_quota_guard_config(invalid)

        invalid = copy.deepcopy(QUOTA_GUARD_DEFAULTS)
        invalid["extra"] = True
        with self.assertRaises(SchedulerError) as caught:
            validate_quota_guard_config(invalid)
        self.assertEqual(caught.exception.details["unknown"], ["extra"])

    def test_enabled_guard_requires_all_fixed_capabilities(self):
        value = config(":memory:")
        value["quota_guard"] = {**value["quota_guard"], "enabled": True}
        value["capabilities"].remove("quota_guard.local.write")
        with self.assertRaises(SchedulerError) as caught:
            validate_config(value)
        self.assertEqual(caught.exception.code, "CAPABILITY_DENIED")
        self.assertEqual(caught.exception.details["missing"], ["quota_guard.local.write"])

    def test_plan_normalizes_and_sorts_targets_with_supplied_limits(self):
        limits = {
            "min_check_interval_seconds": 120,
            "max_check_interval_seconds": 900,
            "max_targets": 2,
        }
        self.assertEqual(
            validate_quota_guard_plan(quota_guard_plan(), limits=limits),
            {
                "schema_version": "1",
                "threshold_remaining_percent": 20.0,
                "check_interval_seconds": 120,
                "target_thread_ids": ["thread-a", "thread-z"],
                "resume_non_goal_threads": False,
            },
        )

    def test_plan_accepts_a_full_config_and_uses_quota_guard_limits(self):
        value = config(":memory:")
        value["quota_guard"] = {
            "enabled": True,
            "min_check_interval_seconds": 300,
            "max_check_interval_seconds": 600,
            "max_targets": 1,
            "resume_hysteresis_percent": 2.0,
        }
        validated = validate_config(value)
        plan = quota_guard_plan(check_interval_seconds=600, target_thread_ids=["thread-1"])
        self.assertEqual(
            validate_quota_guard_plan(plan, validated)["target_thread_ids"],
            ["thread-1"],
        )

        plan["check_interval_seconds"] = 120
        with self.assertRaises(SchedulerError):
            validate_quota_guard_plan(plan, validated)

    def test_plan_rejects_bounds_duplicates_unknown_fields_and_unsafe_targets(self):
        for field, bad_value in (
            ("threshold_remaining_percent", -0.1),
            ("threshold_remaining_percent", 100.1),
            ("check_interval_seconds", 59),
            ("check_interval_seconds", 3601),
        ):
            with self.subTest(field=field, bad_value=bad_value):
                with self.assertRaises(SchedulerError):
                    validate_quota_guard_plan(
                        quota_guard_plan(**{field: bad_value}), QUOTA_GUARD_DEFAULTS
                    )

        with self.assertRaises(SchedulerError):
            validate_quota_guard_plan(
                quota_guard_plan(target_thread_ids=["thread-a", "thread-a"]),
                QUOTA_GUARD_DEFAULTS,
            )
        with self.assertRaises(SchedulerError) as caught:
            validate_quota_guard_plan(
                quota_guard_plan(prompt="must not be executable content"),
                QUOTA_GUARD_DEFAULTS,
            )
        self.assertEqual(caught.exception.details["unknown"], ["prompt"])

        for target in ("thread title", "account@example.invalid", {"objective": "x"}):
            with self.subTest(target=target):
                with self.assertRaises(SchedulerError):
                    validate_quota_guard_plan(
                        quota_guard_plan(target_thread_ids=[target]),
                        QUOTA_GUARD_DEFAULTS,
                    )

        with self.assertRaises(SchedulerError):
            validate_quota_guard_plan(
                quota_guard_plan(target_thread_ids=[]), QUOTA_GUARD_DEFAULTS
            )
        with self.assertRaises(SchedulerError):
            validate_quota_guard_plan(
                quota_guard_plan(target_thread_ids=["thread-%d" % i for i in range(101)]),
                QUOTA_GUARD_DEFAULTS,
            )

    def test_published_config_example_and_new_schemas_expose_only_contract_fields(self):
        root = Path(__file__).resolve().parents[1]
        example = json.loads((root / "scheduler.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["quota_guard"], QUOTA_GUARD_DEFAULTS)
        self.assertTrue(
            {
                "quota_guard.read",
                "quota_guard.local.write",
                "quota_guard.thread.control",
            }.issubset(example["capabilities"])
        )

        config_schema = json.loads(
            (root / "schemas/config.schema.json").read_text(encoding="utf-8")
        )
        capability_values = config_schema["properties"]["capabilities"]["items"]["enum"]
        self.assertTrue(
            {
                "quota_guard.read",
                "quota_guard.local.write",
                "quota_guard.thread.control",
            }.issubset(capability_values)
        )
        self.assertNotIn("quota_guard", config_schema["required"])

        plan_schema = json.loads(
            (root / "schemas/quota-guard-plan.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(plan_schema["properties"]),
            {
                "schema_version",
                "threshold_remaining_percent",
                "check_interval_seconds",
                "target_thread_ids",
                "resume_non_goal_threads",
            },
        )
        self.assertFalse(plan_schema["additionalProperties"])

        session_schema = json.loads(
            (root / "schemas/quota-guard-session.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(session_schema["properties"]),
            {
                "schema_version",
                "guard_id",
                "state",
                "threshold_remaining_percent",
                "check_interval_seconds",
                "target_count",
                "next_check_at",
                "last_checked_at",
                "reason_code",
                "created_at",
                "updated_at",
            },
        )
        self.assertFalse(session_schema["additionalProperties"])
        self.assertEqual(
            set(session_schema["properties"]["state"]["enum"]),
            {"ARMED", "STOPPING", "HELD_QUOTA", "RESUMING", "NEEDS_REVIEW", "DISARMED"},
        )


if __name__ == "__main__":
    unittest.main()
