"""The only Phase B runner: deterministic simulation with no side effects."""

from typing import Any, Dict

from .errors import SchedulerError


class FakeRunner:
    name = "phase_b_fake_runner"

    def run(self, job: Dict[str, Any]) -> Dict[str, Any]:
        simulation = job.get("simulation")
        if simulation is None:
            actual = dict(job["expected_usage"])
            outcome = "success"
        else:
            actual = dict(simulation["actual_usage"])
            outcome = simulation["outcome"]
        if outcome == "failure":
            raise SchedulerError(
                "SIMULATION_FAILED",
                "The fake runner injected a deterministic failure",
                retryable=False,
            )
        return {
            "actual_usage": actual,
            "dry_run": True,
            "runner": self.name,
            "simulated": True,
            "work_ref": job["work_ref"],
        }
