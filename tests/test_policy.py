import unittest

from codex_work_scheduler.policy import evaluate_policy
from codex_work_scheduler.validation import validate_snapshot

from tests.helpers import NOW, job, policy, snapshot


class PolicyTests(unittest.TestCase):
    def test_both_windows_must_preserve_their_reserve(self) -> None:
        decision = evaluate_policy(
            validate_snapshot(snapshot(five_used=80, weekly_used=20)),
            policy(),
            now=NOW,
            expected_usage=job(five_expected=9, weekly_expected=1)["expected_usage"],
        )
        self.assertFalse(decision["eligible"])
        self.assertIn("FIVE_HOUR_RESERVE", decision["reasons"])
        self.assertNotIn("WEEKLY_RESERVE", decision["reasons"])

        decision = evaluate_policy(
            validate_snapshot(snapshot(five_used=20, weekly_used=85)),
            policy(),
            now=NOW,
            expected_usage=job(five_expected=1, weekly_expected=5)["expected_usage"],
        )
        self.assertFalse(decision["eligible"])
        self.assertIn("WEEKLY_RESERVE", decision["reasons"])

    def test_estimate_multiplier_is_applied_before_admission(self) -> None:
        decision = evaluate_policy(
            validate_snapshot(snapshot(five_used=80, weekly_used=20)),
            policy(estimate_multiplier=2.0),
            now=NOW,
            expected_usage=job(five_expected=6, weekly_expected=1)["expected_usage"],
        )
        self.assertEqual(decision["windows"]["five_hour"]["guarded_estimate_percent"], 12.0)
        self.assertFalse(decision["eligible"])

    def test_stale_observation_or_passed_reset_fails_closed(self) -> None:
        stale = validate_snapshot(snapshot(observed_at=NOW - 301))
        decision = evaluate_policy(stale, policy(), now=NOW)
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["signal_status"], "stale")

        passed_reset = validate_snapshot(snapshot(five_reset=NOW))
        decision = evaluate_policy(passed_reset, policy(), now=NOW)
        self.assertFalse(decision["eligible"])
        self.assertIn("SIGNAL_STALE", decision["reasons"])

    def test_credit_signal_never_adds_headroom(self) -> None:
        value = snapshot(five_used=91)
        value["credit_signal"] = "present"
        value["paid_credit_state"] = "available"
        decision = evaluate_policy(validate_snapshot(value), policy(), now=NOW)
        self.assertFalse(decision["eligible"])
        self.assertIn("FIVE_HOUR_RESERVE", decision["reasons"])


if __name__ == "__main__":
    unittest.main()
