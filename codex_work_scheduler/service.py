"""Agent-native controller with simulation, a guarded canary, and approved work dispatch."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .errors import SchedulerError
from .fake_runner import FakeRunner
from .live_test import CodexLiveTestRunner
from .monitor import CodexThreadMonitor
from .policy import evaluate_policy, has_signal_failure, invalid_signal_decision
from .probe import CodexAppServerProbe
from .store import Store
from .util import canonical_json, new_id, payload_hash
from .validation import (
    ensure_profile,
    require_approval,
    validate_approval,
    validate_job,
    validate_identifier,
    validate_policy,
    validate_snapshot,
    validate_work_package,
)
from .work_runner import CodexWorkRunner


class Controller:
    def __init__(
        self,
        config: Dict[str, Any],
        store: Store,
        *,
        clock=time.time,
        runner: Optional[FakeRunner] = None,
        probe: Optional[CodexAppServerProbe] = None,
        live_test_runner: Optional[CodexLiveTestRunner] = None,
        monitor: Optional[CodexThreadMonitor] = None,
        work_runner: Optional[CodexWorkRunner] = None,
    ) -> None:
        if config.get("dry_run") is not True:
            raise SchedulerError("DRY_RUN_REQUIRED", "The general work controller requires dry_run to be true")
        self.config = config
        self.store = store
        self.clock = clock
        self.runner = runner or FakeRunner()
        self.probe = probe or CodexAppServerProbe(clock=clock)
        self.live_test_runner = live_test_runner or CodexLiveTestRunner(clock=clock)
        self.monitor = monitor or CodexThreadMonitor(clock=clock)
        self.work_runner = work_runner or CodexWorkRunner(clock=clock)
        self.store.bootstrap(policy=config["policy"], now=self.clock())

    def _account_binding_view(self) -> Dict[str, Any]:
        binding = self.store.account_binding(self.config["profile_key"])
        if binding is None:
            return {"bound": False}
        return {
            "account_type": binding["account_type"],
            "bound": True,
            "fingerprint_prefix": binding["account_fingerprint"][:12],
            "first_observed_at": binding["first_observed_at"],
            "last_observed_at": binding["last_observed_at"],
            "plan_type": binding["plan_type"],
        }

    def _evaluate_current_policy(
        self,
        snapshot: Optional[Dict[str, Any]],
        policy: Dict[str, Any],
        *,
        now: float,
        expected_usage: Optional[Dict[str, float]] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> Dict[str, Any]:
        if snapshot is None:
            return evaluate_policy(None, policy, now=now, expected_usage=expected_usage)
        try:
            validated = validate_snapshot(snapshot)
            ensure_profile(validated, self.config)
        except SchedulerError as exc:
            reason = "PROFILE_MISMATCH" if exc.code == "PROFILE_MISMATCH" else "SIGNAL_INVALID"
            if "account_fingerprint" not in snapshot or "account_type" not in snapshot:
                reason = "ACCOUNT_IDENTITY_UNAVAILABLE"
            return invalid_signal_decision(reason)
        if connection is None:
            binding = self.store.account_binding(self.config["profile_key"])
        else:
            binding = self.store.account_binding_in(connection, self.config["profile_key"])
        if binding is None:
            return invalid_signal_decision("ACCOUNT_IDENTITY_UNAVAILABLE")
        if (
            binding["account_fingerprint"] != validated["account_fingerprint"]
            or binding["account_type"] != validated["account_type"]
        ):
            return invalid_signal_decision("PROFILE_MISMATCH")
        return evaluate_policy(
            validated,
            policy,
            now=now,
            expected_usage=expected_usage,
        )

    @staticmethod
    def _actor(value: str) -> str:
        return validate_identifier(value, "actor")

    def _require_capability(self, capability: str) -> None:
        if capability not in self.config["capabilities"]:
            raise SchedulerError(
                "CAPABILITY_DENIED",
                "The configured agent lacks the required capability",
                details={"required": capability},
            )

    @staticmethod
    def _insert_approval(connection: sqlite3.Connection, approval: Dict[str, Any]) -> None:
        approval_digest = payload_hash(approval)
        try:
            connection.execute(
                """INSERT INTO approvals(
                       approval_id, action, actor, scope_hash, capabilities_json,
                       granted_at, expires_at, approval_hash
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval["approval_id"],
                    approval["action"],
                    approval["actor"],
                    approval["scope_hash"],
                    canonical_json(approval["capabilities"]),
                    approval["granted_at"],
                    approval["expires_at"],
                    approval_digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise SchedulerError(
                "APPROVAL_ALREADY_USED",
                "The approval identifier has already been consumed",
            ) from exc

    @staticmethod
    def _with_replay(result: Dict[str, Any], replayed: bool) -> Dict[str, Any]:
        value = dict(result)
        value["idempotent_replay"] = replayed
        return value

    def status(self) -> Dict[str, Any]:
        self._require_capability("control.read")
        controller = self.store.controller()
        policy_record = self.store.policy()
        snapshot = self.store.latest_snapshot()
        decision = self._evaluate_current_policy(
            snapshot, policy_record["policy"], now=self.clock()
        )
        effective_mode = controller["mode"]
        if effective_mode == "READY" and has_signal_failure(decision):
            effective_mode = "BLOCKED"
        return {
            "controller": controller,
            "account_binding": self._account_binding_view(),
            "counts": self.store.counts(now=self.clock()),
            "dry_run": True,
            "effective_mode": effective_mode,
            "execution_modes": {
                "dispatch": "guarded_live" if self.config["dispatch"]["enabled"] else "disabled",
                "tick": "fake_only",
            },
            "paid_credit_policy": {
                "account_binding_required": True,
                "contrary_signal_blocks": True,
                "verification_mode": self.config["dispatch"]["credit_verification_mode"],
            },
            "phase": "D_GUARDED_DISPATCH",
            "policy": {
                "policy_hash": policy_record["policy_hash"],
                "version": policy_record["version"],
            },
            "quota": {
                "decision": decision,
                "snapshot": snapshot,
            },
        }

    def queue_list(self) -> Dict[str, Any]:
        self._require_capability("queue.read")
        return {"jobs": self.store.list_jobs()}

    def queue_get(self, job_id: str) -> Dict[str, Any]:
        self._require_capability("queue.read")
        return self.store.get_job(job_id)

    def _resolve_workspace_cwd(self, cwd_value: str) -> str:
        roots = [Path(item).resolve() for item in self.config["workspace_roots"]]
        candidate = Path(cwd_value)
        if not candidate.is_absolute():
            candidate = roots[0] / candidate
        resolved = candidate.resolve()
        if not resolved.is_dir():
            raise SchedulerError("PATH_DENIED", "The work package cwd is not a directory")
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise SchedulerError("PATH_DENIED", "The work package cwd is outside workspace_roots")
        return str(resolved)

    def _normalize_work_package(self, value: Dict[str, Any]) -> Dict[str, Any]:
        package = validate_work_package(value)
        package["execution"]["cwd"] = self._resolve_workspace_cwd(
            package["execution"]["cwd"]
        )
        return package

    def queue_proposals(self) -> Dict[str, Any]:
        self._require_capability("queue.read")
        return {"proposals": self.store.list_proposals()}

    def queue_proposal_get(self, job_id: str) -> Dict[str, Any]:
        self._require_capability("queue.read")
        return self.store.get_proposal(job_id)

    def queue_propose(
        self,
        package_value: Dict[str, Any],
        *,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        self._require_capability("backlog.local.write")
        actor = self._actor(actor)
        now = self.clock()
        package = self._normalize_work_package(package_value)
        package_digest = payload_hash(package)
        request = {"package": package}

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            job = package["job"]
            try:
                connection.execute(
                    """INSERT INTO job_proposals(
                           job_id, work_ref, priority, state, package_json, package_hash,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, 'proposed', ?, ?, ?, ?)""",
                    (
                        job["job_id"],
                        job["work_ref"],
                        job["priority"],
                        canonical_json(package),
                        package_digest,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SchedulerError(
                    "PROPOSAL_ALREADY_EXISTS",
                    "The job or package is already present in the backlog",
                ) from exc
            event_id = self.store.append_audit(
                connection,
                event_type="backlog.proposed",
                actor=actor,
                details={
                    "job_id": job["job_id"],
                    "package_hash": package_digest,
                    "priority": job["priority"],
                },
                now=now,
            )
            return {
                "audit_event_id": event_id,
                "job_id": job["job_id"],
                "package_hash": package_digest,
                "state": "proposed",
            }

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="queue.propose",
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def queue_approve(
        self,
        job_id: str,
        approval_value: Dict[str, Any],
        *,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        self._require_capability("queue.local.write")
        actor = self._actor(actor)
        now = self.clock()
        proposal = self.store.get_proposal(validate_identifier(job_id, "job_id"))
        package = validate_work_package(proposal["package"])
        if payload_hash(package) != proposal["package_hash"]:
            raise SchedulerError("STATE_INVALID", "The stored work package hash does not match")
        self._resolve_workspace_cwd(package["execution"]["cwd"])
        approval = validate_approval(approval_value, now=now, allow_expired=True)
        request = {"approval": approval, "job_id": job_id, "package_hash": proposal["package_hash"]}
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="queue.approve",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)
        approval = validate_approval(approval_value, now=now)
        require_approval(
            approval,
            action="queue.approve",
            scope_hash=proposal["package_hash"],
            capability="work.dispatch",
        )

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            row = connection.execute(
                "SELECT state FROM job_proposals WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise SchedulerError("PROPOSAL_NOT_FOUND", "The proposal no longer exists")
            if row["state"] != "proposed":
                raise SchedulerError("INVALID_TRANSITION", "Only proposed work can be approved")
            self._insert_approval(connection, approval)
            job = package["job"]
            try:
                connection.execute(
                    """INSERT INTO jobs(
                           job_id, work_ref, priority, state, manifest_json, manifest_hash,
                           approval_id, created_at, updated_at
                       ) VALUES (?, ?, ?, 'approved', ?, ?, ?, ?, ?)""",
                    (
                        job["job_id"],
                        job["work_ref"],
                        job["priority"],
                        canonical_json(job),
                        payload_hash(job),
                        approval["approval_id"],
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SchedulerError("JOB_ALREADY_EXISTS", "The job identifier already exists") from exc
            connection.execute(
                """UPDATE job_proposals
                   SET state = 'approved', approved_at = ?, updated_at = ?
                   WHERE job_id = ?""",
                (now, now, job_id),
            )
            event_id = self.store.append_audit(
                connection,
                event_type="backlog.approved",
                actor=actor,
                details={
                    "approval_id": approval["approval_id"],
                    "job_id": job_id,
                    "package_hash": proposal["package_hash"],
                },
                now=now,
            )
            return {
                "approval_id": approval["approval_id"],
                "audit_event_id": event_id,
                "job_id": job_id,
                "state": "approved",
            }

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="queue.approve",
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def queue_submit(
        self,
        job_value: Dict[str, Any],
        approval_value: Dict[str, Any],
        *,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        self._require_capability("queue.local.write")
        actor = self._actor(actor)
        now = self.clock()
        job = validate_job(job_value)
        approval = validate_approval(approval_value, now=now, allow_expired=True)
        request = {"approval": approval, "job": job}
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="queue.submit",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)
        approval = validate_approval(approval_value, now=now)
        scope = payload_hash(job)
        require_approval(
            approval,
            action="queue.submit",
            scope_hash=scope,
            capability="queue.local.write",
        )

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            self._insert_approval(connection, approval)
            try:
                connection.execute(
                    """INSERT INTO jobs(
                           job_id, work_ref, priority, state, manifest_json, manifest_hash,
                           approval_id, created_at, updated_at
                       ) VALUES (?, ?, ?, 'approved', ?, ?, ?, ?, ?)""",
                    (
                        job["job_id"],
                        job["work_ref"],
                        job["priority"],
                        canonical_json(job),
                        scope,
                        approval["approval_id"],
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise SchedulerError("JOB_ALREADY_EXISTS", "The job identifier already exists") from exc
            event_id = self.store.append_audit(
                connection,
                event_type="queue.submitted",
                actor=actor,
                details={
                    "approval_id": approval["approval_id"],
                    "job_id": job["job_id"],
                    "manifest_hash": scope,
                    "priority": job["priority"],
                },
                now=now,
            )
            return {
                "approval_id": approval["approval_id"],
                "audit_event_id": event_id,
                "dry_run": True,
                "job_id": job["job_id"],
                "state": "approved",
            }

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="queue.submit",
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def queue_cancel(self, job_id: str, *, idempotency_key: str, actor: str) -> Dict[str, Any]:
        self._require_capability("queue.local.write")
        actor = self._actor(actor)
        now = self.clock()
        request = {"job_id": job_id}

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            row = connection.execute("SELECT state FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise SchedulerError("JOB_NOT_FOUND", "The requested job does not exist")
            if row["state"] == "simulating":
                raise SchedulerError("JOB_LEASED", "A leased job requires reconciliation")
            if row["state"] not in {
                "approved",
                "held_dependency",
                "held_policy",
                "held_schedule",
                "held_capability",
                "needs_review",
            }:
                raise SchedulerError("INVALID_TRANSITION", "The job cannot be cancelled from its current state")
            connection.execute(
                "UPDATE jobs SET state = 'cancelled', updated_at = ? WHERE job_id = ?", (now, job_id)
            )
            event_id = self.store.append_audit(
                connection,
                event_type="queue.cancelled",
                actor=actor,
                details={"job_id": job_id},
                now=now,
            )
            return {"audit_event_id": event_id, "job_id": job_id, "state": "cancelled"}

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="queue.cancel",
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def policy_show(self) -> Dict[str, Any]:
        self._require_capability("policy.read")
        return self.store.policy()

    def policy_check(
        self,
        *,
        snapshot_value: Optional[Dict[str, Any]] = None,
        job_value: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._require_capability("policy.evaluate")
        snapshot = validate_snapshot(snapshot_value) if snapshot_value is not None else self.store.latest_snapshot()
        if snapshot_value is not None:
            ensure_profile(snapshot, self.config)
        job = validate_job(job_value) if job_value is not None else None
        expected = job["expected_usage"] if job is not None else None
        policy = self.store.policy()["policy"]
        if snapshot_value is not None:
            return evaluate_policy(snapshot, policy, now=self.clock(), expected_usage=expected)
        return self._evaluate_current_policy(
            snapshot,
            policy,
            now=self.clock(),
            expected_usage=expected,
        )

    def policy_set(
        self,
        policy_value: Dict[str, Any],
        approval_value: Dict[str, Any],
        *,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        self._require_capability("policy.local.write")
        actor = self._actor(actor)
        now = self.clock()
        policy = validate_policy(policy_value)
        approval = validate_approval(approval_value, now=now, allow_expired=True)
        request = {"approval": approval, "policy": policy}
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="policy.set",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)
        approval = validate_approval(approval_value, now=now)
        scope = payload_hash(policy)
        require_approval(
            approval,
            action="policy.set",
            scope_hash=scope,
            capability="policy.local.write",
        )

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            self._insert_approval(connection, approval)
            row = connection.execute("SELECT version FROM policies WHERE singleton = 1").fetchone()
            version = int(row["version"]) + 1
            connection.execute(
                """UPDATE policies SET version = ?, policy_json = ?, policy_hash = ?, updated_at = ?
                   WHERE singleton = 1""",
                (version, canonical_json(policy), scope, now),
            )
            self.store.set_controller(
                connection,
                mode="PAUSED",
                reason_code="POLICY_CHANGED",
                now=now,
            )
            event_id = self.store.append_audit(
                connection,
                event_type="policy.updated",
                actor=actor,
                details={"approval_id": approval["approval_id"], "policy_hash": scope, "version": version},
                now=now,
            )
            return {
                "approval_id": approval["approval_id"],
                "audit_event_id": event_id,
                "policy_hash": scope,
                "version": version,
            }

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="policy.set",
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def preflight(
        self,
        *,
        action: Optional[str] = None,
        input_value: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._require_capability("policy.evaluate")
        now = self.clock()
        controller = self.store.controller()
        policy = self.store.policy()["policy"]
        snapshot = self.store.latest_snapshot()
        decision = self._evaluate_current_policy(snapshot, policy, now=now)
        candidates = self.store.reconciliation_candidates(now=now)
        run_candidates = self.store.run_reconciliation_candidates(now=now)
        issues = list(decision["reasons"])
        integrity = self.store.integrity()
        audit_integrity = self.store.verify_audit()
        if not integrity["valid"]:
            issues.append("DATABASE_INVALID")
        if not audit_integrity["valid"]:
            issues.append("AUDIT_INVALID")
        if candidates or run_candidates:
            issues.append("RECONCILE_REQUIRED")
        elif self.store.counts(now=now)["active_leases"]:
            issues.append("LEASE_ACTIVE")
        if any(run["state"] in {"starting", "running"} for run in self.store.list_runs()):
            issues.append("RUN_ACTIVE")
        approval_requirement = None
        if action is not None:
            if action == "queue.submit":
                if input_value is None:
                    raise SchedulerError("INVALID_ARGUMENT", "queue.submit preflight requires a job input")
                scoped_value = validate_job(input_value)
                capability = "queue.local.write"
            elif action == "queue.approve":
                if input_value is None:
                    raise SchedulerError("INVALID_ARGUMENT", "queue.approve preflight requires a work package input")
                scoped_value = self._normalize_work_package(input_value)
                capability = "work.dispatch"
            elif action == "policy.set":
                if input_value is None:
                    raise SchedulerError("INVALID_ARGUMENT", "policy.set preflight requires a policy input")
                scoped_value = validate_policy(input_value)
                capability = "policy.local.write"
            elif action == "resume":
                if input_value is not None:
                    raise SchedulerError("INVALID_ARGUMENT", "resume preflight does not accept an input")
                scoped_value = {"action": "resume", "target_mode": "READY"}
                capability = "control.local.write"
            elif action == "reconcile":
                if input_value is not None:
                    raise SchedulerError("INVALID_ARGUMENT", "reconcile preflight does not accept an input")
                scoped_value = {
                    "action": "reconcile",
                    "lease_ids": [item["lease_id"] for item in candidates],
                    "run_ids": [item["run_id"] for item in run_candidates],
                }
                capability = "reconcile.local"
            elif action == "live-test.run":
                if input_value is not None:
                    raise SchedulerError("INVALID_ARGUMENT", "live-test.run preflight does not accept an input")
                scoped_value = self._live_test_scope()
                capability = "live_test.dispatch"
            else:
                raise SchedulerError("INVALID_ARGUMENT", "The preflight action is unsupported")
            self._require_capability(capability)
            approval_requirement = {
                "action": action,
                "capability": capability,
                "scope_hash": payload_hash(scoped_value),
            }
        return {
            "approval_requirement": approval_requirement,
            "controller_mode": controller["mode"],
            "dry_run": True,
            "issues": sorted(set(issues)),
            "integrity": {"audit": audit_integrity, "database": integrity},
            "policy_decision": decision,
            "reconciliation_candidates": [item["lease_id"] for item in candidates],
            "run_reconciliation_candidates": [item["run_id"] for item in run_candidates],
            "safe_to_resume": not issues,
        }

    def approval_prepare(
        self,
        *,
        action: str,
        input_value: Optional[Dict[str, Any]],
        requested_approver: str,
        suggested_ttl_seconds: int,
    ) -> Dict[str, Any]:
        requested_approver = self._actor(requested_approver)
        if suggested_ttl_seconds < 60 or suggested_ttl_seconds > 86_400:
            raise SchedulerError(
                "INVALID_ARGUMENT",
                "The suggested approval lifetime must be between 60 and 86400 seconds",
            )
        preflight = self.preflight(action=action, input_value=input_value)
        requirement = preflight["approval_requirement"]
        if requirement is None:
            raise SchedulerError("STATE_INVALID", "The approval requirement is missing")
        created_at = self.clock()
        request_id = new_id("apr")
        return {
            "approval_request": {
                "schema_version": "1",
                "request_id": request_id,
                "action": requirement["action"],
                "scope_hash": requirement["scope_hash"],
                "capability": requirement["capability"],
                "created_at": created_at,
                "requested_approver": requested_approver,
                "suggested_ttl_seconds": suggested_ttl_seconds,
            },
            "approval_template": {
                "schema_version": "1",
                "approval_id": None,
                "actor": requested_approver,
                "action": requirement["action"],
                "scope_hash": requirement["scope_hash"],
                "capabilities": [requirement["capability"]],
                "granted_at": None,
                "expires_at": None,
            },
            "dry_run": True,
            "preflight": preflight,
            "template_is_approval": False,
        }

    def _set_mode(
        self,
        *,
        mode: str,
        reason_code: str,
        idempotency_key: str,
        actor: str,
        command: str,
    ) -> Dict[str, Any]:
        self._require_capability("control.local.write")
        actor = self._actor(actor)
        reason_code = validate_identifier(reason_code, "reason_code")
        now = self.clock()
        request = {"mode": mode, "reason_code": reason_code}

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            current = connection.execute("SELECT mode FROM controller WHERE singleton = 1").fetchone()["mode"]
            self.store.set_controller(connection, mode=mode, reason_code=reason_code, now=now)
            event_id = self.store.append_audit(
                connection,
                event_type="controller.%s" % command,
                actor=actor,
                details={"from": current, "reason_code": reason_code, "to": mode},
                now=now,
            )
            return {"audit_event_id": event_id, "from": current, "mode": mode, "reason_code": reason_code}

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command=command,
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def pause(self, *, reason_code: str, idempotency_key: str, actor: str) -> Dict[str, Any]:
        return self._set_mode(
            mode="PAUSED",
            reason_code=reason_code,
            idempotency_key=idempotency_key,
            actor=actor,
            command="pause",
        )

    def stop(self, *, reason_code: str, idempotency_key: str, actor: str) -> Dict[str, Any]:
        return self._set_mode(
            mode="STOPPED",
            reason_code=reason_code,
            idempotency_key=idempotency_key,
            actor=actor,
            command="stop",
        )

    def resume(
        self,
        approval_value: Dict[str, Any],
        *,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        self._require_capability("control.local.write")
        actor = self._actor(actor)
        now = self.clock()
        approval = validate_approval(approval_value, now=now, allow_expired=True)
        scope_value = {"action": "resume", "target_mode": "READY"}
        request = {"approval": approval, "scope": scope_value}
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="resume",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)
        approval = validate_approval(approval_value, now=now)
        scope = payload_hash(scope_value)
        require_approval(
            approval,
            action="resume",
            scope_hash=scope,
            capability="control.local.write",
        )
        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            active_leases = connection.execute(
                "SELECT lease_id, expires_at FROM leases WHERE state = 'active' ORDER BY expires_at ASC"
            ).fetchall()
            if any(row["expires_at"] <= now for row in active_leases):
                raise SchedulerError("RECONCILE_REQUIRED", "Expired leases require reconciliation")
            if active_leases:
                raise SchedulerError("LEASE_ACTIVE", "An active lease prevents resume", retryable=True)
            active_runs = connection.execute(
                "SELECT run_id FROM runs WHERE state IN ('starting', 'running') ORDER BY started_at"
            ).fetchall()
            if active_runs:
                raise SchedulerError("RUN_ACTIVE", "An active run prevents resume", retryable=True)
            policy_row = connection.execute(
                "SELECT policy_json FROM policies WHERE singleton = 1"
            ).fetchone()
            decision = self._evaluate_current_policy(
                self.store.latest_snapshot_in(connection),
                json.loads(policy_row["policy_json"]),
                now=now,
                connection=connection,
            )
            if not decision["eligible"]:
                code = decision["reasons"][0] if decision["reasons"] else "POLICY_DENIED"
                raise SchedulerError(
                    code,
                    "Resume preflight failed",
                    retryable=code in {"SIGNAL_MISSING", "SIGNAL_STALE"},
                )
            self._insert_approval(connection, approval)
            current = connection.execute("SELECT mode FROM controller WHERE singleton = 1").fetchone()["mode"]
            if current == "READY":
                raise SchedulerError("INVALID_TRANSITION", "The controller is already ready")
            self.store.set_controller(connection, mode="READY", reason_code=None, now=now)
            event_id = self.store.append_audit(
                connection,
                event_type="controller.resumed",
                actor=actor,
                details={"approval_id": approval["approval_id"], "from": current, "to": "READY"},
                now=now,
            )
            return {"approval_id": approval["approval_id"], "audit_event_id": event_id, "from": current, "mode": "READY"}

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="resume",
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def reconcile_plan(self) -> Dict[str, Any]:
        self._require_capability("reconcile.local")
        candidates = self.store.reconciliation_candidates(now=self.clock())
        run_candidates = self.store.run_reconciliation_candidates(now=self.clock())
        scope_value = {
            "action": "reconcile",
            "lease_ids": [item["lease_id"] for item in candidates],
            "run_ids": [item["run_id"] for item in run_candidates],
        }
        return {
            "approval_requirement": {
                "action": "reconcile",
                "capability": "reconcile.local",
                "scope_hash": payload_hash(scope_value),
            },
            "candidates": candidates,
            "run_candidates": run_candidates,
            "dry_run": True,
        }

    def reconcile(
        self,
        approval_value: Dict[str, Any],
        *,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        self._require_capability("reconcile.local")
        actor = self._actor(actor)
        now = self.clock()
        approval = validate_approval(approval_value, now=now, allow_expired=True)
        request = {"approval": approval}
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="reconcile",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)
        approval = validate_approval(approval_value, now=now)
        candidates = self.store.reconciliation_candidates(now=now)
        run_candidates = self.store.run_reconciliation_candidates(now=now)
        scope_value = {
            "action": "reconcile",
            "lease_ids": [item["lease_id"] for item in candidates],
            "run_ids": [item["run_id"] for item in run_candidates],
        }
        scope = payload_hash(scope_value)
        require_approval(
            approval,
            action="reconcile",
            scope_hash=scope,
            capability="reconcile.local",
        )

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            self._insert_approval(connection, approval)
            reconciled: List[str] = []
            reconciled_runs: List[str] = []
            for candidate in candidates:
                changed = connection.execute(
                    "UPDATE leases SET state = 'expired' WHERE lease_id = ? AND state = 'active' AND expires_at <= ?",
                    (candidate["lease_id"], now),
                ).rowcount
                if changed:
                    connection.execute(
                        "UPDATE jobs SET state = 'needs_review', updated_at = ? WHERE job_id = ?",
                        (now, candidate["job_id"]),
                    )
                    reconciled.append(candidate["lease_id"])
            for candidate in run_candidates:
                run_row = connection.execute(
                    "SELECT job_id FROM runs WHERE run_id = ?", (candidate["run_id"],)
                ).fetchone()
                changed = connection.execute(
                    """UPDATE runs SET state = 'needs_review', updated_at = ?,
                              completed_at = ?, stop_reason = 'LEASE_EXPIRED'
                       WHERE run_id = ? AND state IN ('starting', 'running')
                             AND lease_expires_at <= ?""",
                    (now, now, candidate["run_id"], now),
                ).rowcount
                if changed:
                    if run_row is not None and run_row["job_id"] is not None:
                        connection.execute(
                            "UPDATE jobs SET state = 'needs_review', updated_at = ? WHERE job_id = ?",
                            (now, run_row["job_id"]),
                        )
                    reconciled_runs.append(candidate["run_id"])
            if reconciled or reconciled_runs:
                self.store.set_controller(
                    connection,
                    mode="PAUSED",
                    reason_code="RECONCILIATION_REVIEW_REQUIRED",
                    now=now,
                )
            event_id = self.store.append_audit(
                connection,
                event_type="controller.reconciled",
                actor=actor,
                details={
                    "approval_id": approval["approval_id"],
                    "lease_ids": reconciled,
                    "run_ids": reconciled_runs,
                },
                now=now,
            )
            current_mode = connection.execute(
                "SELECT mode FROM controller WHERE singleton = 1"
            ).fetchone()["mode"]
            return {
                "approval_id": approval["approval_id"],
                "audit_event_id": event_id,
                "controller_mode": current_mode,
                "reconciled_lease_ids": reconciled,
                "reconciled_run_ids": reconciled_runs,
            }

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="reconcile",
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def background_recover(self, *, actor: str) -> Dict[str, Any]:
        """Fail closed around expired work without ever restarting it."""
        self._require_capability("background.run")
        self._require_capability("reconcile.local")
        actor = self._actor(actor)
        now = self.clock()
        with self.store.transaction() as connection:
            if not self.store.verify_audit_in(connection)["valid"]:
                raise SchedulerError("AUDIT_INVALID", "Crash recovery requires a valid audit chain")
            reconciled_leases: List[str] = []
            reconciled_runs: List[str] = []
            lease_rows = connection.execute(
                """SELECT lease_id, job_id FROM leases
                   WHERE state = 'active' AND expires_at <= ?
                   ORDER BY expires_at ASC, lease_id ASC""",
                (now,),
            ).fetchall()
            for row in lease_rows:
                changed = connection.execute(
                    """UPDATE leases SET state = 'expired', completed_at = ?
                       WHERE lease_id = ? AND state = 'active' AND expires_at <= ?""",
                    (now, row["lease_id"], now),
                ).rowcount
                if changed:
                    connection.execute(
                        "UPDATE jobs SET state = 'needs_review', updated_at = ? WHERE job_id = ?",
                        (now, row["job_id"]),
                    )
                    reconciled_leases.append(row["lease_id"])
            run_rows = connection.execute(
                """SELECT run_id, job_id FROM runs
                   WHERE state IN ('starting', 'running') AND lease_expires_at <= ?
                   ORDER BY lease_expires_at ASC, run_id ASC""",
                (now,),
            ).fetchall()
            for row in run_rows:
                changed = connection.execute(
                    """UPDATE runs SET state = 'needs_review', updated_at = ?, completed_at = ?,
                              stop_reason = 'LEASE_EXPIRED'
                       WHERE run_id = ? AND state IN ('starting', 'running')
                             AND lease_expires_at <= ?""",
                    (now, now, row["run_id"], now),
                ).rowcount
                if changed:
                    if row["job_id"] is not None:
                        connection.execute(
                            "UPDATE jobs SET state = 'needs_review', updated_at = ? WHERE job_id = ?",
                            (now, row["job_id"]),
                        )
                    reconciled_runs.append(row["run_id"])
            event_id = None
            if reconciled_leases or reconciled_runs:
                self.store.set_controller(
                    connection,
                    mode="PAUSED",
                    reason_code="RECOVERY_REVIEW_REQUIRED",
                    now=now,
                )
                event_id = self.store.append_audit(
                    connection,
                    event_type="background.recovered",
                    actor=actor,
                    details={
                        "lease_ids": reconciled_leases,
                        "run_ids": reconciled_runs,
                    },
                    now=now,
                )
        return {
            "audit_event_id": event_id,
            "reconciled_lease_ids": reconciled_leases,
            "reconciled_run_ids": reconciled_runs,
            "requires_review": bool(reconciled_leases or reconciled_runs),
        }

    def background_safety_stop(self, *, reason_code: str, actor: str) -> Dict[str, Any]:
        self._require_capability("background.run")
        self._require_capability("control.local.write")
        actor = self._actor(actor)
        reason_code = validate_identifier(reason_code, "reason_code")
        now = self.clock()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT mode, reason_code FROM controller WHERE singleton = 1"
            ).fetchone()
            if current["mode"] == "BLOCKED" and current["reason_code"] == reason_code:
                return {
                    "audit_event_id": None,
                    "from": current["mode"],
                    "mode": "BLOCKED",
                    "reason_code": reason_code,
                }
            self.store.set_controller(
                connection,
                mode="BLOCKED",
                reason_code=reason_code,
                now=now,
            )
            audit_event_id = None
            if self.store.verify_audit_in(connection)["valid"]:
                audit_event_id = self.store.append_audit(
                    connection,
                    event_type="background.safety_stopped",
                    actor=actor,
                    details={"from": current["mode"], "reason_code": reason_code},
                    now=now,
                )
        return {
            "audit_event_id": audit_event_id,
            "from": current["mode"],
            "mode": "BLOCKED",
            "reason_code": reason_code,
        }

    def background_dispatch_failed(
        self,
        *,
        job_id: str,
        reason_code: str,
        actor: str,
    ) -> Dict[str, Any]:
        """Prevent an attempted unattended dispatch from becoming retryable."""
        self._require_capability("background.run")
        actor = self._actor(actor)
        job_id = validate_identifier(job_id, "job_id")
        reason_code = validate_identifier(reason_code, "reason_code")
        now = self.clock()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise SchedulerError("JOB_NOT_FOUND", "The attempted background job is missing")
            previous_state = row["state"]
            changed = connection.execute(
                """UPDATE jobs SET state = 'needs_review', updated_at = ?
                   WHERE job_id = ? AND state IN (
                       'approved', 'held_policy', 'held_dependency',
                       'held_schedule', 'held_capability'
                   )""",
                (now, job_id),
            ).rowcount
            target_mode = (
                "BLOCKED" if reason_code in {"AUDIT_INVALID", "DATABASE_INVALID"} else "PAUSED"
            )
            self.store.set_controller(
                connection,
                mode=target_mode,
                reason_code=reason_code if target_mode == "BLOCKED" else "WORK_REVIEW_REQUIRED",
                now=now,
            )
            event_id = None
            if self.store.verify_audit_in(connection)["valid"]:
                event_id = self.store.append_audit(
                    connection,
                    event_type="background.dispatch_failed",
                    actor=actor,
                    details={
                        "failure_code": reason_code,
                        "job_id": job_id,
                        "job_marked_review": changed == 1,
                        "previous_state": previous_state,
                    },
                    now=now,
                )
        return {
            "audit_event_id": event_id,
            "job_id": job_id,
            "marked_needs_review": changed == 1,
            "previous_state": previous_state,
            "reason_code": reason_code,
        }

    def probe_quota(self, *, idempotency_key: str, actor: str) -> Dict[str, Any]:
        self._require_capability("quota.read")
        actor = self._actor(actor)
        started_at = self.clock()
        request = {"limit_id": self.config["limit_id"], "profile_key": self.config["profile_key"]}

        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="probe",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)

        probed_snapshot = validate_snapshot(
            self.probe.read(
                profile_key=self.config["profile_key"],
                limit_id=self.config["limit_id"],
                account_fingerprint_key=self.store.account_fingerprint_key(),
            )
        )
        ensure_profile(probed_snapshot, self.config)

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            snapshot = probed_snapshot
            previous = self.store.latest_snapshot_in(connection)
            binding = self.store.account_binding_in(connection, self.config["profile_key"])
            binding_created = binding is None
            if binding is None:
                connection.execute(
                    """INSERT INTO account_bindings(
                           profile_key, account_fingerprint, account_type, plan_type,
                           first_observed_at, last_observed_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot["profile_key"],
                        snapshot["account_fingerprint"],
                        snapshot["account_type"],
                        snapshot["plan_type"],
                        snapshot["observed_at"],
                        snapshot["observed_at"],
                    ),
                )
            elif (
                binding["account_fingerprint"] != snapshot["account_fingerprint"]
                or binding["account_type"] != snapshot["account_type"]
                or (
                    binding["plan_type"] is not None
                    and snapshot["plan_type"] is not None
                    and binding["plan_type"] != snapshot["plan_type"]
                )
            ):
                raise SchedulerError(
                    "PROFILE_MISMATCH",
                    "The configured profile is bound to another ChatGPT account",
                )
            else:
                connection.execute(
                    """UPDATE account_bindings SET last_observed_at = ?
                       WHERE profile_key = ?""",
                    (snapshot["observed_at"], snapshot["profile_key"]),
                )
            if previous is not None and previous["limit_id"] != snapshot["limit_id"]:
                raise SchedulerError("PROFILE_MISMATCH", "The selected rate-limit bucket changed for this profile")
            if (
                previous is not None
                and previous.get("plan_type") is not None
                and snapshot.get("plan_type") is not None
                and previous["plan_type"] != snapshot["plan_type"]
            ):
                raise SchedulerError("PROFILE_MISMATCH", "The observed plan changed for the configured profile")
            resets: List[str] = []
            if previous is not None:
                for name in ("five_hour", "weekly"):
                    if (
                        snapshot[name]["resets_at"] > previous[name]["resets_at"]
                        and snapshot[name]["used_percent"] < previous[name]["used_percent"]
                    ):
                        resets.append(name)
            snapshot_digest = payload_hash(snapshot)
            snapshot_id = new_id("qta")
            connection.execute(
                """INSERT INTO quota_snapshots(
                       snapshot_id, observed_at, profile_key, limit_id, plan_type,
                       snapshot_json, snapshot_hash, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    snapshot["observed_at"],
                    snapshot["profile_key"],
                    snapshot["limit_id"],
                    snapshot["plan_type"],
                    canonical_json(snapshot),
                    snapshot_digest,
                    self.clock(),
                ),
            )
            event_id = self.store.append_audit(
                connection,
                event_type="quota.observed",
                actor=actor,
                details={
                    "account_binding_created": binding_created,
                    "limit_id": snapshot["limit_id"],
                    "reset_windows": resets,
                    "snapshot_hash": snapshot_digest,
                },
                now=self.clock(),
            )
            return {
                "audit_event_id": event_id,
                "account_binding": {
                    "account_type": snapshot["account_type"],
                    "bound": True,
                    "fingerprint_prefix": snapshot["account_fingerprint"][:12],
                },
                "outbound_methods": [
                    "initialize",
                    "initialized",
                    "account/read",
                    "account/rateLimits/read",
                ],
                "reset_windows": resets,
                "snapshot": snapshot,
                "snapshot_id": snapshot_id,
            }

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="probe",
            request=request,
            now=started_at,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def cycle(self, *, idempotency_key: str, actor: str) -> Dict[str, Any]:
        """Run one idempotent observe-evaluate-simulate cycle with no auto-resume."""
        actor = self._actor(actor)
        now = self.clock()
        request = {
            "dry_run": True,
            "limit_id": self.config["limit_id"],
            "profile_key": self.config["profile_key"],
        }
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="cycle",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)

        probe_key = "cycle-probe-%s" % payload_hash(
            {"cycle": idempotency_key, "step": "probe"}
        )[:48]
        tick_key = "cycle-tick-%s" % payload_hash(
            {"cycle": idempotency_key, "step": "tick"}
        )[:48]
        probe_result = self.probe_quota(idempotency_key=probe_key, actor=actor)
        preflight = self.preflight()
        tick_result: Optional[Dict[str, Any]] = None
        if preflight["controller_mode"] != "READY":
            outcome = "skipped_controller_not_ready"
        elif not preflight["safe_to_resume"]:
            outcome = "skipped_preflight_denied"
        else:
            tick_result = self.tick(idempotency_key=tick_key, actor=actor)
            if tick_result.get("simulated"):
                outcome = "simulated"
            elif tick_result.get("requires_review"):
                outcome = "needs_review"
            elif tick_result.get("blocked"):
                outcome = "blocked"
            else:
                outcome = "no_eligible_job"
        cycle_result = {
            "dry_run": True,
            "outcome": outcome,
            "preflight": preflight,
            "probe": probe_result,
            "tick": tick_result,
        }

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="cycle",
            request=request,
            now=now,
            operation=lambda _connection: cycle_result,
        )
        return self._with_replay(result, replayed)

    def tick(self, *, idempotency_key: str, actor: str) -> Dict[str, Any]:
        self._require_capability("simulate.local")
        actor = self._actor(actor)
        now = self.clock()
        request = {"dry_run": True}

        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="tick",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)

        selected: Optional[Dict[str, Any]] = None
        selected_decision: Optional[Dict[str, Any]] = None
        lease_id: Optional[str] = None
        snapshot: Optional[Dict[str, Any]] = None
        policy: Optional[Dict[str, Any]] = None

        # Commit the lease before the fake runner starts. A process crash then
        # leaves durable evidence for reconcile instead of rolling the claim back.
        with self.store.transaction() as connection:
            controller = connection.execute("SELECT mode FROM controller WHERE singleton = 1").fetchone()
            if controller["mode"] != "READY":
                raise SchedulerError("CONTROLLER_NOT_READY", "The controller is not ready")
            active = connection.execute("SELECT COUNT(*) AS count FROM leases WHERE state = 'active'").fetchone()["count"]
            policy_record = connection.execute("SELECT policy_json FROM policies WHERE singleton = 1").fetchone()
            policy = json.loads(policy_record["policy_json"])
            if active >= policy["max_concurrency"]:
                raise SchedulerError("CONCURRENCY_LIMIT", "The simulation concurrency limit is reached", retryable=True)
            snapshot = self.store.latest_snapshot_in(connection)
            signal_decision = self._evaluate_current_policy(
                snapshot,
                policy,
                now=now,
                connection=connection,
            )
            if has_signal_failure(signal_decision):
                self.store.set_controller(connection, mode="BLOCKED", reason_code=signal_decision["reasons"][0], now=now)
                event_id = self.store.append_audit(
                    connection,
                    event_type="controller.blocked",
                    actor=actor,
                    details={"reason_codes": signal_decision["reasons"]},
                    now=now,
                )
                result = {
                    "audit_event_id": event_id,
                    "blocked": True,
                    "dry_run": True,
                    "policy_decision": signal_decision,
                    "simulated": False,
                }
                self.store.record_idempotent(
                    connection,
                    key=idempotency_key,
                    command="tick",
                    request=request,
                    result=result,
                    now=now,
                )
                return self._with_replay(result, False)
            rows = connection.execute(
                """SELECT jobs.job_id, jobs.manifest_json, job_proposals.package_json
                   FROM jobs
                   LEFT JOIN job_proposals ON job_proposals.job_id = jobs.job_id
                   WHERE jobs.state IN ('approved', 'held_policy', 'held_dependency', 'held_schedule')
                   ORDER BY jobs.priority DESC, jobs.created_at ASC, jobs.job_id ASC"""
            ).fetchall()
            denied: List[Dict[str, Any]] = []
            for row in rows:
                job = json.loads(row["manifest_json"])
                if row["package_json"] is not None:
                    package = json.loads(row["package_json"])
                    if package["not_before"] is not None and package["not_before"] > now:
                        denied.append({"job_id": job["job_id"], "reasons": ["NOT_BEFORE"]})
                        connection.execute(
                            "UPDATE jobs SET state = 'held_schedule', updated_at = ? WHERE job_id = ?",
                            (now, job["job_id"]),
                        )
                        continue
                    dependencies_ready = True
                    for dependency_id in package["dependencies"]:
                        dependency = connection.execute(
                            "SELECT state FROM jobs WHERE job_id = ?", (dependency_id,)
                        ).fetchone()
                        if dependency is None or dependency["state"] not in {"simulated", "succeeded"}:
                            dependencies_ready = False
                            break
                    if not dependencies_ready:
                        denied.append({"job_id": job["job_id"], "reasons": ["DEPENDENCY_PENDING"]})
                        connection.execute(
                            "UPDATE jobs SET state = 'held_dependency', updated_at = ? WHERE job_id = ?",
                            (now, job["job_id"]),
                        )
                        continue
                decision = self._evaluate_current_policy(
                    snapshot,
                    policy,
                    now=now,
                    expected_usage=job["expected_usage"],
                    connection=connection,
                )
                if decision["eligible"]:
                    selected = job
                    selected_decision = decision
                    break
                denied.append({"job_id": job["job_id"], "reasons": decision["reasons"]})
                connection.execute(
                    "UPDATE jobs SET state = 'held_policy', updated_at = ? WHERE job_id = ?",
                    (now, job["job_id"]),
                )
            if selected is None:
                result = {
                    "blocked": False,
                    "denied_jobs": denied,
                    "dry_run": True,
                    "simulated": False,
                }
                self.store.record_idempotent(
                    connection,
                    key=idempotency_key,
                    command="tick",
                    request=request,
                    result=result,
                    now=now,
                )
                return self._with_replay(result, False)

            lease_id = new_id("lse")
            expires_at = now + policy["lease_seconds"]
            connection.execute(
                """INSERT INTO leases(lease_id, job_id, owner, state, acquired_at, expires_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (lease_id, selected["job_id"], actor, now, expires_at),
            )
            connection.execute(
                "UPDATE jobs SET state = 'simulating', updated_at = ? WHERE job_id = ?",
                (now, selected["job_id"]),
            )
            self.store.append_audit(
                connection,
                event_type="simulation.started",
                actor=actor,
                details={"job_id": selected["job_id"], "lease_id": lease_id},
                now=now,
            )

        # No subprocess or work adapter is reachable here. FakeRunner only
        # transforms the validated simulation manifest.
        runner_error: Optional[SchedulerError] = None
        runner_result: Optional[Dict[str, Any]] = None
        try:
            runner_result = self.runner.run(selected)
        except SchedulerError as exc:
            runner_error = exc

        finished_at = self.clock()
        with self.store.transaction() as connection:
            lease = connection.execute(
                "SELECT state FROM leases WHERE lease_id = ? AND job_id = ?",
                (lease_id, selected["job_id"]),
            ).fetchone()
            if lease is None or lease["state"] != "active":
                raise SchedulerError("LEASE_LOST", "The simulation lease is no longer active")
            if runner_error is not None:
                connection.execute(
                    "UPDATE leases SET state = 'failed', completed_at = ? WHERE lease_id = ?",
                    (finished_at, lease_id),
                )
                connection.execute(
                    "UPDATE jobs SET state = 'needs_review', updated_at = ? WHERE job_id = ?",
                    (finished_at, selected["job_id"]),
                )
                self.store.set_controller(
                    connection,
                    mode="BLOCKED",
                    reason_code=runner_error.code,
                    now=finished_at,
                )
                event_id = self.store.append_audit(
                    connection,
                    event_type="simulation.failed",
                    actor=actor,
                    details={
                        "failure_code": runner_error.code,
                        "job_id": selected["job_id"],
                        "lease_id": lease_id,
                    },
                    now=finished_at,
                )
                result = {
                    "audit_event_id": event_id,
                    "dry_run": True,
                    "failure_code": runner_error.code,
                    "job_id": selected["job_id"],
                    "lease_id": lease_id,
                    "requires_review": True,
                    "simulated": False,
                }
                self.store.record_idempotent(
                    connection,
                    key=idempotency_key,
                    command="tick",
                    request=request,
                    result=result,
                    now=finished_at,
                )
                return self._with_replay(result, False)

            if runner_result is None or policy is None or snapshot is None:
                raise SchedulerError("STATE_INVALID", "The simulation result is incomplete")
            actual = runner_result["actual_usage"]
            expected = selected["expected_usage"]
            overrun = (
                actual["five_hour_percent"] > expected["five_hour_percent"]
                or actual["weekly_percent"] > expected["weekly_percent"]
            )
            post_actual_safe = (
                snapshot["five_hour"]["remaining_percent"] - actual["five_hour_percent"]
                >= policy["five_hour_reserve_percent"]
                and snapshot["weekly"]["remaining_percent"] - actual["weekly_percent"]
                >= policy["weekly_reserve_percent"]
            )
            requires_review = overrun or not post_actual_safe
            final_job_state = "needs_review" if requires_review else "simulated"
            final_lease_state = "completed_overrun" if requires_review else "completed"
            connection.execute(
                "UPDATE leases SET state = ?, completed_at = ? WHERE lease_id = ?",
                (final_lease_state, finished_at, lease_id),
            )
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (final_job_state, finished_at, selected["job_id"]),
            )
            if requires_review:
                self.store.set_controller(
                    connection,
                    mode="PAUSED",
                    reason_code="SIMULATION_OVERRUN",
                    now=finished_at,
                )
            event_id = self.store.append_audit(
                connection,
                event_type="simulation.completed",
                actor=actor,
                details={
                    "job_id": selected["job_id"],
                    "lease_id": lease_id,
                    "overrun": overrun,
                    "requires_review": requires_review,
                },
                now=finished_at,
            )
            result = {
                "audit_event_id": event_id,
                "dry_run": True,
                "job_id": selected["job_id"],
                "lease_id": lease_id,
                "overrun": overrun,
                "policy_decision": selected_decision,
                "requires_review": requires_review,
                "runner_result": runner_result,
                "simulated": True,
                "state": final_job_state,
            }
            self.store.record_idempotent(
                connection,
                key=idempotency_key,
                command="tick",
                request=request,
                result=result,
                now=finished_at,
            )
            return self._with_replay(result, False)

    def _live_test_scope(self) -> Dict[str, Any]:
        live_test = self.config["live_test"]
        policy_record = self.store.policy()
        return {
            "action": "live-test.run",
            "canary_version": "1",
            "effort": live_test["effort"],
            "expected_usage": live_test["expected_usage"],
            "max_runtime_seconds": live_test["max_runtime_seconds"],
            "model": live_test["model"],
            "policy_hash": policy_record["policy_hash"],
            "workspace_root": str(Path(self.config["workspace_roots"][0]).resolve()),
        }

    def live_test_preflight(self) -> Dict[str, Any]:
        self._require_capability("live_test.dispatch")
        now = self.clock()
        live_test = self.config["live_test"]
        controller = self.store.controller()
        policy_record = self.store.policy()
        snapshot = self.store.latest_snapshot()
        decision = self._evaluate_current_policy(
            snapshot,
            policy_record["policy"],
            now=now,
            expected_usage=live_test["expected_usage"],
        )
        issues = list(decision["reasons"])
        if not live_test["enabled"]:
            issues.append("LIVE_TEST_DISABLED")
        if controller["mode"] != "PAUSED":
            issues.append("CONTROLLER_NOT_PAUSED")
        counts = self.store.counts(now=now)
        if counts["active_leases"]:
            issues.append("LEASE_ACTIVE")
        if any(run["state"] in {"starting", "running"} for run in self.store.list_runs()):
            issues.append("LIVE_TEST_ACTIVE")
        run_candidates = self.store.run_reconciliation_candidates(now=now)
        if run_candidates:
            issues.append("RECONCILE_REQUIRED")
        integrity = self.store.integrity()
        audit_integrity = self.store.verify_audit()
        if not integrity["valid"]:
            issues.append("DATABASE_INVALID")
        if not audit_integrity["valid"]:
            issues.append("AUDIT_INVALID")
        if snapshot is None or snapshot.get("paid_credit_state") == "unknown":
            issues.append("PAID_CREDIT_SIGNAL_UNVERIFIED")
        elif snapshot.get("paid_credit_state") == "available":
            issues.append("PAID_CREDITS_AVAILABLE")
        if snapshot is not None and snapshot.get("spend_control_state") == "reached":
            issues.append("SPEND_CONTROL_REACHED")
        issues = sorted(set(issues))
        scope = self._live_test_scope()
        return {
            "approval_requirement": {
                "action": "live-test.run",
                "capability": "live_test.dispatch",
                "scope_hash": payload_hash(scope),
            },
            "controller_mode": controller["mode"],
            "issues": issues,
            "live_test": {
                "canary_version": scope["canary_version"],
                "effort": scope["effort"],
                "expected_usage": scope["expected_usage"],
                "max_runtime_seconds": scope["max_runtime_seconds"],
                "model": scope["model"],
                "network_access": False,
                "sandbox": "read-only",
            },
            "policy_decision": decision,
            "safe_to_dispatch": not issues,
        }

    def live_test_run(
        self,
        approval_value: Dict[str, Any],
        *,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        self._require_capability("live_test.dispatch")
        actor = self._actor(actor)
        started_at = self.clock()
        approval = validate_approval(approval_value, now=started_at, allow_expired=True)
        scope = self._live_test_scope()
        request = {"approval": approval, "scope": scope}
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="live-test.run",
            request=request,
        )
        if replay is not None:
            run_id = replay.get("run_id")
            if run_id:
                replay = {**replay, "run": self.store.get_run(run_id)}
            return self._with_replay(replay, True)
        approval = validate_approval(approval_value, now=started_at)
        require_approval(
            approval,
            action="live-test.run",
            scope_hash=payload_hash(scope),
            capability="live_test.dispatch",
        )

        probe_key = "live-preflight-%s" % payload_hash(
            {"operation": idempotency_key, "step": "probe"}
        )[:48]
        self.probe_quota(idempotency_key=probe_key, actor=actor)
        preflight = self.live_test_preflight()
        if not preflight["safe_to_dispatch"]:
            code = preflight["issues"][0] if preflight["issues"] else "LIVE_TEST_DENIED"
            raise SchedulerError(code, "The live-test preflight stopped dispatch", details={"issues": preflight["issues"]})

        run_id = new_id("run")
        lease_expires_at = started_at + self.config["live_test"]["max_runtime_seconds"] + 15
        initial_result = {
            "approval_id": approval["approval_id"],
            "live_dispatch": True,
            "run_id": run_id,
            "state": "starting",
        }
        with self.store.transaction() as connection:
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE state IN ('starting', 'running')"
            ).fetchone()["count"]
            if active:
                raise SchedulerError("LIVE_TEST_ACTIVE", "Another live test is active", retryable=True)
            self._insert_approval(connection, approval)
            connection.execute(
                """INSERT INTO runs(
                       run_id, job_id, kind, state, thread_id, turn_id, started_at,
                       updated_at, lease_expires_at
                   ) VALUES (?, NULL, 'canary', 'starting', NULL, NULL, ?, ?, ?)""",
                (run_id, started_at, started_at, lease_expires_at),
            )
            event_id = self.store.append_audit(
                connection,
                event_type="live_test.claimed",
                actor=actor,
                details={
                    "approval_id": approval["approval_id"],
                    "run_id": run_id,
                    "scope_hash": payload_hash(scope),
                },
                now=started_at,
            )
            initial_result["audit_event_id"] = event_id
            self.store.record_idempotent(
                connection,
                key=idempotency_key,
                command="live-test.run",
                request=request,
                result=initial_result,
                now=started_at,
            )

        def on_started(thread_id: str, turn_id: str) -> None:
            now = self.clock()
            with self.store.transaction() as connection:
                connection.execute(
                    """UPDATE runs SET state = 'running', thread_id = ?, turn_id = ?, updated_at = ?
                       WHERE run_id = ? AND state = 'starting'""",
                    (thread_id, turn_id, now, run_id),
                )
                self.store.append_audit(
                    connection,
                    event_type="live_test.started",
                    actor=actor,
                    details={"run_id": run_id, "thread_id": thread_id, "turn_id": turn_id},
                    now=now,
                )

        safety_counter = {"value": 0}

        def safety_check() -> bool:
            safety_counter["value"] += 1
            if safety_counter["value"] > 1:
                safety_probe_key = "live-safety-%s-%d" % (
                    payload_hash({"operation": idempotency_key})[:40],
                    safety_counter["value"],
                )
                try:
                    self.probe_quota(idempotency_key=safety_probe_key, actor=actor)
                except Exception:
                    return False
            check = self.live_test_preflight()
            return check["safe_to_dispatch"] or check["issues"] == ["LIVE_TEST_ACTIVE"]

        runner_error: Optional[SchedulerError] = None
        runner_result: Dict[str, Any]
        live_config = self.config["live_test"]
        try:
            runner_result = self.live_test_runner.run(
                cwd=scope["workspace_root"],
                model=live_config["model"],
                effort=live_config["effort"],
                max_runtime_seconds=live_config["max_runtime_seconds"],
                poll_interval_seconds=live_config["poll_interval_seconds"],
                safety_check=safety_check,
                on_started=on_started,
            )
        except SchedulerError as exc:
            runner_error = exc
            current = self.store.get_run(run_id)
            runner_result = {
                "state": "needs_review" if current["turn_id"] else "failed",
                "stop_reason": exc.code,
                "thread_id": current["thread_id"],
                "turn_id": current["turn_id"],
            }
        except Exception:
            runner_error = SchedulerError(
                "LIVE_TEST_INTERNAL_FAILURE",
                "The canary failed without a verified terminal state",
            )
            current = self.store.get_run(run_id)
            runner_result = {
                "state": "needs_review" if current["turn_id"] else "failed",
                "stop_reason": runner_error.code,
                "thread_id": current["thread_id"],
                "turn_id": current["turn_id"],
            }

        finished_at = self.clock()
        final_result = {
            "approval_id": approval["approval_id"],
            "live_dispatch": True,
            "run_id": run_id,
            "state": runner_result["state"],
            "stop_reason": runner_result["stop_reason"],
            "thread_id": runner_result["thread_id"],
            "turn_id": runner_result["turn_id"],
        }
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE runs SET state = ?, thread_id = COALESCE(thread_id, ?),
                          turn_id = COALESCE(turn_id, ?), updated_at = ?, completed_at = ?,
                          stop_reason = ? WHERE run_id = ?""",
                (
                    runner_result["state"],
                    runner_result["thread_id"],
                    runner_result["turn_id"],
                    finished_at,
                    finished_at,
                    runner_result["stop_reason"],
                    run_id,
                ),
            )
            event_id = self.store.append_audit(
                connection,
                event_type="live_test.completed" if runner_error is None else "live_test.failed",
                actor=actor,
                details={
                    "failure_code": runner_error.code if runner_error else None,
                    "run_id": run_id,
                    "state": runner_result["state"],
                    "stop_reason": runner_result["stop_reason"],
                    "thread_id": runner_result["thread_id"],
                    "turn_id": runner_result["turn_id"],
                },
                now=finished_at,
            )
            final_result["audit_event_id"] = event_id
            connection.execute(
                "UPDATE idempotency SET result_json = ? WHERE idempotency_key = ?",
                (canonical_json(final_result), idempotency_key),
            )
        return self._with_replay(final_result, False)

    def _select_dispatch_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        now: float,
        job_id: Optional[str],
    ) -> Dict[str, Any]:
        parameters: List[Any] = []
        where = ""
        if job_id is not None:
            where = "WHERE jobs.job_id = ?"
            parameters.append(job_id)
        rows = connection.execute(
            """SELECT jobs.job_id, jobs.state, jobs.manifest_hash, jobs.approval_id,
                      proposals.state AS proposal_state, proposals.package_json,
                      proposals.package_hash, approvals.capabilities_json
               FROM jobs
               LEFT JOIN job_proposals AS proposals ON proposals.job_id = jobs.job_id
               LEFT JOIN approvals ON approvals.approval_id = jobs.approval_id
               %s
               ORDER BY jobs.priority DESC, jobs.created_at ASC, jobs.job_id ASC""" % where,
            tuple(parameters),
        ).fetchall()
        if job_id is not None and not rows:
            return {"candidate": None, "issues": ["JOB_NOT_FOUND"]}
        aggregate_issues: List[str] = []
        dispatchable_states = {
            "approved",
            "held_policy",
            "held_dependency",
            "held_schedule",
            "held_capability",
        }
        policy_row = connection.execute(
            "SELECT policy_json FROM policies WHERE singleton = 1"
        ).fetchone()
        policy = json.loads(policy_row["policy_json"])
        snapshot = self.store.latest_snapshot_in(connection)
        for row in rows:
            issues: List[str] = []
            if row["state"] not in dispatchable_states:
                issues.append("JOB_NOT_DISPATCHABLE")
            if (
                row["package_json"] is None
                or row["package_hash"] is None
                or row["proposal_state"] != "approved"
                or row["capabilities_json"] is None
            ):
                issues.append("WORK_AUTHORIZATION_MISSING")
                aggregate_issues.extend(issues)
                if job_id is not None:
                    break
                continue
            try:
                package = validate_work_package(json.loads(row["package_json"]))
                capabilities = json.loads(row["capabilities_json"])
            except (SchedulerError, TypeError, ValueError):
                issues.append("WORK_PACKAGE_INVALID")
                aggregate_issues.extend(issues)
                if job_id is not None:
                    break
                continue
            if not isinstance(capabilities, list) or "work.dispatch" not in capabilities:
                issues.append("WORK_AUTHORIZATION_MISSING")
            if payload_hash(package) != row["package_hash"]:
                issues.append("WORK_PACKAGE_HASH_MISMATCH")
            if payload_hash(package["job"]) != row["manifest_hash"]:
                issues.append("WORK_MANIFEST_HASH_MISMATCH")
            if package["job"]["job_id"] != row["job_id"]:
                issues.append("WORK_PACKAGE_IDENTITY_MISMATCH")
            try:
                resolved_cwd = self._resolve_workspace_cwd(package["execution"]["cwd"])
            except SchedulerError:
                issues.append("WORKSPACE_DENIED")
                resolved_cwd = ""
            if package["not_before"] is not None and package["not_before"] > now:
                issues.append("NOT_BEFORE_PENDING")
            for dependency_id in package["dependencies"]:
                dependency = connection.execute(
                    "SELECT state FROM jobs WHERE job_id = ?", (dependency_id,)
                ).fetchone()
                if dependency is None or dependency["state"] not in {"simulated", "succeeded"}:
                    issues.append("DEPENDENCY_PENDING")
                    break
            decision = self._evaluate_current_policy(
                snapshot,
                policy,
                now=now,
                expected_usage=package["job"]["expected_usage"],
                connection=connection,
            )
            issues.extend(decision["reasons"])
            if not issues:
                return {
                    "candidate": {
                        "approval_id": row["approval_id"],
                        "cwd": resolved_cwd,
                        "decision": decision,
                        "job_id": row["job_id"],
                        "package": package,
                        "package_hash": row["package_hash"],
                    },
                    "issues": [],
                }
            aggregate_issues.extend(issues)
            if job_id is not None:
                break
        if not aggregate_issues:
            aggregate_issues.append("NO_ELIGIBLE_JOB")
        elif job_id is None:
            aggregate_issues.append("NO_ELIGIBLE_JOB")
        return {"candidate": None, "issues": sorted(set(aggregate_issues))}

    def _paid_credit_issues(self, snapshot: Optional[Dict[str, Any]]) -> List[str]:
        if snapshot is None:
            return ["PAID_CREDIT_SIGNAL_UNVERIFIED"]
        if snapshot.get("paid_credit_state") == "available":
            return ["PAID_CREDITS_AVAILABLE"]
        if snapshot.get("spend_control_state") == "reached":
            return ["SPEND_CONTROL_REACHED"]
        if (
            self.config["dispatch"]["credit_verification_mode"]
            == "operator_attested_subscription_only"
        ):
            return []
        if snapshot.get("paid_credit_state") == "unknown":
            return ["PAID_CREDIT_SIGNAL_UNVERIFIED"]
        if snapshot.get("spend_control_state") != "not_reached":
            return ["SPEND_CONTROL_SIGNAL_UNVERIFIED"]
        return []

    def dispatch_preflight(self, *, job_id: Optional[str] = None) -> Dict[str, Any]:
        self._require_capability("work.dispatch")
        if job_id is not None:
            job_id = validate_identifier(job_id, "job_id")
        now = self.clock()
        controller = self.store.controller()
        issues: List[str] = []
        if not self.config["dispatch"]["enabled"]:
            issues.append("DISPATCH_DISABLED")
        if controller["mode"] != "READY":
            issues.append("CONTROLLER_NOT_READY")
        counts = self.store.counts(now=now)
        if counts["active_leases"]:
            issues.append("LEASE_ACTIVE")
        if any(run["state"] in {"starting", "running"} for run in self.store.list_runs()):
            issues.append("RUN_ACTIVE")
        if self.store.reconciliation_candidates(now=now) or self.store.run_reconciliation_candidates(now=now):
            issues.append("RECONCILE_REQUIRED")
        integrity = self.store.integrity()
        audit_integrity = self.store.verify_audit()
        if not integrity["valid"]:
            issues.append("DATABASE_INVALID")
        if not audit_integrity["valid"]:
            issues.append("AUDIT_INVALID")
        snapshot = self.store.latest_snapshot()
        issues.extend(self._paid_credit_issues(snapshot))
        with self.store.reader() as connection:
            selection = self._select_dispatch_candidate(connection, now=now, job_id=job_id)
        issues.extend(selection["issues"])
        candidate = selection["candidate"]
        return {
            "candidate": None
            if candidate is None
            else {
                "approval_id": candidate["approval_id"],
                "job_id": candidate["job_id"],
                "package_hash": candidate["package_hash"],
            },
            "controller_mode": controller["mode"],
            "credit_verification_mode": self.config["dispatch"]["credit_verification_mode"],
            "issues": sorted(set(issues)),
            "policy_decision": candidate["decision"] if candidate is not None else None,
            "safe_to_dispatch": not issues,
        }

    def dispatch_run(
        self,
        *,
        job_id: Optional[str],
        idempotency_key: str,
        actor: str,
        heartbeat: Optional[Callable[[], None]] = None,
        lease_owner: Optional[str] = None,
        shutdown_requested: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, Any]:
        self._require_capability("work.dispatch")
        actor = self._actor(actor)
        if job_id is not None:
            job_id = validate_identifier(job_id, "job_id")
        claim_owner = validate_identifier(
            lease_owner
            or "dispatch-%s" % payload_hash({"idempotency_key": idempotency_key})[:48],
            "lease_owner",
        )
        request = {"job_id": job_id}
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="dispatch.run",
            request=request,
        )
        if replay is not None:
            run_id = replay.get("run_id")
            if run_id:
                replay = {**replay, "run": self.store.get_run(run_id)}
            return self._with_replay(replay, True)

        if shutdown_requested is not None and shutdown_requested():
            raise SchedulerError(
                "SERVICE_SHUTDOWN_REQUESTED",
                "The foreground supervisor is shutting down",
            )
        if heartbeat is not None:
            heartbeat()

        probe_key = "dispatch-preflight-%s" % payload_hash(
            {"operation": idempotency_key, "step": "probe"}
        )[:48]
        self.probe_quota(idempotency_key=probe_key, actor=actor)
        preflight = self.dispatch_preflight(job_id=job_id)
        if not preflight["safe_to_dispatch"]:
            code = preflight["issues"][0] if preflight["issues"] else "DISPATCH_DENIED"
            raise SchedulerError(
                code,
                "The guarded dispatch preflight stopped work",
                details={"issues": preflight["issues"]},
            )
        selected_job_id = preflight["candidate"]["job_id"]
        started_at = self.clock()
        # Capability inventory uses several bounded RPCs before the normal poll
        # loop. Keep the crash-recovery lease longer than their combined timeout.
        heartbeat_seconds = max(90, self.config["dispatch"]["poll_interval_seconds"] * 3)
        lease_expires_at = started_at + heartbeat_seconds
        run_id = new_id("run")
        selected: Dict[str, Any]
        initial_snapshot: Optional[Dict[str, Any]]
        with self.store.transaction() as connection:
            if not self.store.verify_audit_in(connection)["valid"]:
                raise SchedulerError("AUDIT_INVALID", "The audit chain changed before claim")
            database_check = [row[0] for row in connection.execute("PRAGMA quick_check").fetchall()]
            if database_check != ["ok"]:
                raise SchedulerError("DATABASE_INVALID", "The database failed its claim-time check")
            controller = connection.execute(
                "SELECT mode FROM controller WHERE singleton = 1"
            ).fetchone()
            if controller["mode"] != "READY":
                raise SchedulerError("CONTROLLER_NOT_READY", "The controller is not ready")
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE state IN ('starting', 'running')"
            ).fetchone()["count"]
            if active:
                raise SchedulerError("RUN_ACTIVE", "Another live run is active", retryable=True)
            active_leases = connection.execute(
                "SELECT COUNT(*) AS count FROM leases WHERE state = 'active'"
            ).fetchone()["count"]
            if active_leases:
                raise SchedulerError("LEASE_ACTIVE", "A simulation lease is active", retryable=True)
            selection = self._select_dispatch_candidate(
                connection, now=started_at, job_id=selected_job_id
            )
            if selection["candidate"] is None:
                code = selection["issues"][0] if selection["issues"] else "DISPATCH_DENIED"
                raise SchedulerError(code, "The work package became ineligible before claim")
            selected = selection["candidate"]
            initial_snapshot = self.store.latest_snapshot_in(connection)
            paid_issues = self._paid_credit_issues(initial_snapshot)
            if paid_issues:
                raise SchedulerError(paid_issues[0], "The paid-credit gate changed before claim")
            changed = connection.execute(
                "UPDATE jobs SET state = 'running', updated_at = ? WHERE job_id = ? AND state IN ('approved', 'held_policy', 'held_dependency', 'held_schedule', 'held_capability')",
                (started_at, selected_job_id),
            ).rowcount
            if changed != 1:
                raise SchedulerError("JOB_CLAIM_CONFLICT", "The job could not be claimed", retryable=True)
            connection.execute(
                """INSERT INTO runs(
                       run_id, job_id, kind, state, thread_id, turn_id, started_at,
                       updated_at, lease_expires_at, lease_owner
                   ) VALUES (?, ?, 'work', 'starting', NULL, NULL, ?, ?, ?, ?)""",
                (
                    run_id,
                    selected_job_id,
                    started_at,
                    started_at,
                    lease_expires_at,
                    claim_owner,
                ),
            )
            event_id = self.store.append_audit(
                connection,
                event_type="dispatch.claimed",
                actor=actor,
                details={
                    "approval_id": selected["approval_id"],
                    "credit_verification_mode": self.config["dispatch"][
                        "credit_verification_mode"
                    ],
                    "job_id": selected_job_id,
                    "package_hash": selected["package_hash"],
                    "run_id": run_id,
                },
                now=started_at,
            )
            initial_result = {
                "approval_id": selected["approval_id"],
                "audit_event_id": event_id,
                "job_id": selected_job_id,
                "live_dispatch": True,
                "run_id": run_id,
                "state": "starting",
            }
            self.store.record_idempotent(
                connection,
                key=idempotency_key,
                command="dispatch.run",
                request=request,
                result=initial_result,
                now=started_at,
            )

        def on_thread_started(thread_id: str) -> None:
            now = self.clock()
            with self.store.transaction() as connection:
                changed = connection.execute(
                    """UPDATE runs SET thread_id = ?, updated_at = ?, lease_expires_at = ?
                       WHERE run_id = ? AND job_id = ? AND state = 'starting'
                             AND lease_owner = ?""",
                    (
                        thread_id,
                        now,
                        now + heartbeat_seconds,
                        run_id,
                        selected_job_id,
                        claim_owner,
                    ),
                ).rowcount
                if changed != 1:
                    raise SchedulerError("RUN_CLAIM_LOST", "The run claim was lost")
                self.store.append_audit(
                    connection,
                    event_type="dispatch.thread_started",
                    actor=actor,
                    details={"job_id": selected_job_id, "run_id": run_id, "thread_id": thread_id},
                    now=now,
                )

        def on_started(thread_id: str, turn_id: str) -> None:
            now = self.clock()
            with self.store.transaction() as connection:
                changed = connection.execute(
                    """UPDATE runs SET state = 'running', thread_id = ?, turn_id = ?,
                              updated_at = ?, lease_expires_at = ?
                       WHERE run_id = ? AND job_id = ? AND state = 'starting'
                             AND lease_owner = ?""",
                    (
                        thread_id,
                        turn_id,
                        now,
                        now + heartbeat_seconds,
                        run_id,
                        selected_job_id,
                        claim_owner,
                    ),
                ).rowcount
                if changed != 1:
                    raise SchedulerError("RUN_CLAIM_LOST", "The run claim was lost")
                self.store.append_audit(
                    connection,
                    event_type="dispatch.started",
                    actor=actor,
                    details={"job_id": selected_job_id, "run_id": run_id, "thread_id": thread_id, "turn_id": turn_id},
                    now=now,
                )

        safety_counter = {"value": 0}

        def safety_check() -> bool:
            safety_counter["value"] += 1
            if shutdown_requested is not None and shutdown_requested():
                return False
            if heartbeat is not None:
                try:
                    heartbeat()
                except Exception:
                    return False
            if safety_counter["value"] > 1:
                safety_probe_key = "dispatch-safety-%s-%d" % (
                    payload_hash({"operation": idempotency_key})[:40],
                    safety_counter["value"],
                )
                try:
                    self.probe_quota(idempotency_key=safety_probe_key, actor=actor)
                except Exception:
                    return False
            now = self.clock()
            with self.store.transaction() as connection:
                controller = connection.execute(
                    "SELECT mode FROM controller WHERE singleton = 1"
                ).fetchone()
                if controller["mode"] not in {"READY", "PAUSED"}:
                    return False
                run = connection.execute(
                    """SELECT state FROM runs
                       WHERE run_id = ? AND job_id = ? AND lease_owner = ?""",
                    (run_id, selected_job_id, claim_owner),
                ).fetchone()
                if run is None or run["state"] not in {"starting", "running"}:
                    return False
                snapshot = self.store.latest_snapshot_in(connection)
                if self._paid_credit_issues(snapshot):
                    return False
                policy_row = connection.execute(
                    "SELECT policy_json FROM policies WHERE singleton = 1"
                ).fetchone()
                decision = self._evaluate_current_policy(
                    snapshot,
                    json.loads(policy_row["policy_json"]),
                    now=now,
                    expected_usage=selected["package"]["job"]["expected_usage"],
                    connection=connection,
                )
                if not decision["eligible"]:
                    return False
                connection.execute(
                    """UPDATE runs SET lease_expires_at = ?, updated_at = ?
                       WHERE run_id = ? AND lease_owner = ?""",
                    (now + heartbeat_seconds, now, run_id, claim_owner),
                )
            return True

        package = selected["package"]
        runner_error: Optional[SchedulerError] = None
        try:
            runner_result = self.work_runner.run(
                objective=package["objective"],
                cwd=selected["cwd"],
                model=package["execution"]["model"],
                effort=package["execution"]["effort"],
                sandbox=package["execution"]["sandbox"],
                max_runtime_seconds=package["execution"]["max_runtime_seconds"],
                poll_interval_seconds=self.config["dispatch"]["poll_interval_seconds"],
                safety_check=safety_check,
                on_thread_started=on_thread_started,
                on_started=on_started,
            )
        except SchedulerError as exc:
            runner_error = exc
            current = self.store.get_run(run_id)
            runner_result = {
                "state": "needs_review" if current["turn_id"] else "failed",
                "stop_reason": exc.code,
                "thread_id": current["thread_id"],
                "turn_id": current["turn_id"],
            }
        except Exception:
            runner_error = SchedulerError(
                "WORK_RUNNER_INTERNAL_FAILURE",
                "The work runner failed without a verified terminal state",
            )
            current = self.store.get_run(run_id)
            runner_result = {
                "state": "needs_review" if current["turn_id"] else "failed",
                "stop_reason": runner_error.code,
                "thread_id": current["thread_id"],
                "turn_id": current["turn_id"],
            }

        overrun_detected = False
        if runner_result["state"] == "succeeded":
            final_probe_key = "dispatch-final-%s" % payload_hash(
                {"operation": idempotency_key, "step": "final-probe"}
            )[:48]
            try:
                self.probe_quota(idempotency_key=final_probe_key, actor=actor)
                final_snapshot = self.store.latest_snapshot()
                paid_issues = self._paid_credit_issues(final_snapshot)
                final_policy = self.store.policy()["policy"]
                final_decision = self._evaluate_current_policy(
                    final_snapshot, final_policy, now=self.clock()
                )
                if paid_issues or not final_decision["eligible"]:
                    runner_result["state"] = "needs_review"
                    runner_result["stop_reason"] = (
                        paid_issues[0] if paid_issues else "POST_RUN_RESERVE_BREACH"
                    )
                elif initial_snapshot is not None and final_snapshot is not None:
                    expected = package["job"]["expected_usage"]
                    for window, usage_key in (
                        ("five_hour", "five_hour_percent"),
                        ("weekly", "weekly_percent"),
                    ):
                        if final_snapshot[window]["resets_at"] == initial_snapshot[window]["resets_at"]:
                            observed_delta = max(
                                0.0,
                                final_snapshot[window]["used_percent"]
                                - initial_snapshot[window]["used_percent"],
                            )
                            if observed_delta > expected[usage_key] * final_policy["estimate_multiplier"]:
                                overrun_detected = True
                    if overrun_detected:
                        runner_result["state"] = "needs_review"
                        runner_result["stop_reason"] = "USAGE_ESTIMATE_OVERRUN"
            except Exception:
                runner_result["state"] = "needs_review"
                runner_result["stop_reason"] = "POST_RUN_SIGNAL_LOST"

        finished_at = self.clock()
        job_state = "succeeded" if runner_result["state"] == "succeeded" else "needs_review"
        final_result = {
            "approval_id": selected["approval_id"],
            "job_id": selected_job_id,
            "live_dispatch": True,
            "overrun_detected": overrun_detected,
            "run_id": run_id,
            "state": runner_result["state"],
            "stop_reason": runner_result["stop_reason"],
            "thread_id": runner_result["thread_id"],
            "turn_id": runner_result["turn_id"],
        }
        with self.store.transaction() as connection:
            changed = connection.execute(
                """UPDATE runs SET state = ?, thread_id = COALESCE(thread_id, ?),
                          turn_id = COALESCE(turn_id, ?), updated_at = ?, completed_at = ?,
                          stop_reason = ? WHERE run_id = ? AND lease_owner = ?""",
                (
                    runner_result["state"],
                    runner_result["thread_id"],
                    runner_result["turn_id"],
                    finished_at,
                    finished_at,
                    runner_result["stop_reason"],
                    run_id,
                    claim_owner,
                ),
            ).rowcount
            if changed != 1:
                raise SchedulerError("RUN_CLAIM_LOST", "The run lease owner changed before completion")
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (job_state, finished_at, selected_job_id),
            )
            if runner_result["state"] != "succeeded":
                current_mode = connection.execute(
                    "SELECT mode FROM controller WHERE singleton = 1"
                ).fetchone()["mode"]
                if current_mode == "READY":
                    self.store.set_controller(
                        connection,
                        mode="PAUSED",
                        reason_code=runner_result["stop_reason"] or "WORK_REVIEW_REQUIRED",
                        now=finished_at,
                    )
            event_id = self.store.append_audit(
                connection,
                event_type="dispatch.completed" if runner_error is None else "dispatch.failed",
                actor=actor,
                details={
                    "failure_code": runner_error.code if runner_error else None,
                    "job_id": selected_job_id,
                    "job_state": job_state,
                    "overrun_detected": overrun_detected,
                    "run_id": run_id,
                    "state": runner_result["state"],
                    "stop_reason": runner_result["stop_reason"],
                    "thread_id": runner_result["thread_id"],
                    "turn_id": runner_result["turn_id"],
                },
                now=finished_at,
            )
            final_result["audit_event_id"] = event_id
            connection.execute(
                "UPDATE idempotency SET result_json = ? WHERE idempotency_key = ?",
                (canonical_json(final_result), idempotency_key),
            )
        return self._with_replay(final_result, False)

    def monitor_list(self) -> Dict[str, Any]:
        self._require_capability("monitor.read")
        return {"runs": self.store.list_runs()}

    def monitor_get(self, run_id: str) -> Dict[str, Any]:
        self._require_capability("monitor.read")
        return self.store.get_run(validate_identifier(run_id, "run_id"))

    def monitor_refresh(
        self,
        run_id: str,
        *,
        idempotency_key: str,
        actor: str,
    ) -> Dict[str, Any]:
        self._require_capability("monitor.local.write")
        actor = self._actor(actor)
        run_id = validate_identifier(run_id, "run_id")
        now = self.clock()
        request = {"run_id": run_id}
        replay = self.store.replay_idempotent(
            key=idempotency_key,
            command="monitor.refresh",
            request=request,
        )
        if replay is not None:
            return self._with_replay(replay, True)
        run = self.store.get_run(run_id)
        if run["thread_id"] is None:
            raise SchedulerError("MONITOR_UNAVAILABLE", "The run has no Codex task identifier")
        observation = self.monitor.read(run["thread_id"])
        state = run["state"]
        turn_state = observation["latest_turn_status"]
        monitor_stop_reason: Optional[str] = None
        if turn_state is not None:
            state = {
                "completed": "succeeded",
                "failed": "failed",
                "inProgress": "running",
                "interrupted": "interrupted",
            }[turn_state]
            if run["kind"] == "work" and turn_state == "completed" and run["state"] != "succeeded":
                state = "needs_review"
                monitor_stop_reason = "POST_RUN_REVIEW_REQUIRED"
        elif observation["thread_status"] == "systemError":
            state = "needs_review"
            monitor_stop_reason = "THREAD_SYSTEM_ERROR"
        terminal = state in {"succeeded", "failed", "interrupted", "needs_review"}

        def operation(connection: sqlite3.Connection) -> Dict[str, Any]:
            connection.execute(
                """UPDATE runs SET state = ?, updated_at = ?,
                          completed_at = CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE completed_at END,
                          stop_reason = COALESCE(?, stop_reason)
                   WHERE run_id = ?""",
                (state, now, terminal, now, monitor_stop_reason, run_id),
            )
            if run["job_id"] is not None:
                if state == "succeeded":
                    job_state = "succeeded"
                elif state == "running":
                    job_state = "running"
                elif state == "blocked" and run["stop_reason"] == "SAFETY_CHECK_FAILED":
                    job_state = "held_policy"
                elif state == "blocked":
                    job_state = "held_capability"
                else:
                    job_state = "needs_review"
                connection.execute(
                    "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                    (job_state, now, run["job_id"]),
                )
            event_id = self.store.append_audit(
                connection,
                event_type="monitor.refreshed",
                actor=actor,
                details={
                    "run_id": run_id,
                    "state": state,
                    "thread_id": observation["thread_id"],
                    "turn_id": observation["latest_turn_id"],
                },
                now=now,
            )
            return {
                "audit_event_id": event_id,
                "observation": observation,
                "run_id": run_id,
                "state": state,
            }

        result, replayed = self.store.execute_idempotent(
            key=idempotency_key,
            command="monitor.refresh",
            request=request,
            now=now,
            operation=operation,
        )
        return self._with_replay(result, replayed)

    def audit_list(self, *, limit: int) -> Dict[str, Any]:
        self._require_capability("audit.read")
        if limit < 1 or limit > 1000:
            raise SchedulerError("INVALID_ARGUMENT", "Audit limit must be between 1 and 1000")
        return {"events": self.store.list_audit(limit=limit)}

    def audit_verify(self) -> Dict[str, Any]:
        self._require_capability("audit.read")
        return self.store.verify_audit()
