"""SQLite persistence, idempotency, leases, and tamper-evident audit events."""

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .errors import SchedulerError
from .util import canonical_json, new_id, payload_hash


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            parent = Path(path).parent
            parent_existed = parent.exists()
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not parent_existed:
                parent.chmod(0o700)
        self._memory_connection: Optional[sqlite3.Connection] = None
        if path == ":memory:":
            self._memory_connection = self._new_connection(path)
        self.migrate()

    @staticmethod
    def _new_connection(path: str) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            for candidate in (path, path + "-wal", path + "-shm"):
                try:
                    os.chmod(candidate, 0o600)
                except FileNotFoundError:
                    pass
        return connection

    def connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        return self._new_connection(self.path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            if self._memory_connection is None:
                connection.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            if self._memory_connection is None:
                connection.close()

    def migrate(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            """CREATE TABLE IF NOT EXISTS controller (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                mode TEXT NOT NULL,
                reason_code TEXT,
                updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS policies (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL,
                policy_json TEXT NOT NULL,
                policy_hash TEXT NOT NULL,
                updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS quota_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                observed_at REAL NOT NULL,
                profile_key TEXT NOT NULL,
                limit_id TEXT NOT NULL,
                plan_type TEXT,
                snapshot_json TEXT NOT NULL,
                snapshot_hash TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS account_bindings (
                profile_key TEXT PRIMARY KEY,
                account_fingerprint TEXT NOT NULL,
                account_type TEXT NOT NULL,
                plan_type TEXT,
                first_observed_at REAL NOT NULL,
                last_observed_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                capabilities_json TEXT NOT NULL,
                granted_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                approval_hash TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                work_ref TEXT NOT NULL,
                priority INTEGER NOT NULL,
                state TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS job_proposals (
                job_id TEXT PRIMARY KEY,
                work_ref TEXT NOT NULL,
                priority INTEGER NOT NULL,
                state TEXT NOT NULL,
                package_json TEXT NOT NULL,
                package_hash TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                approved_at REAL
            )""",
            """CREATE TABLE IF NOT EXISTS leases (
                lease_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES jobs(job_id),
                owner TEXT NOT NULL,
                state TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                completed_at REAL
            )""",
            """CREATE TABLE IF NOT EXISTS idempotency (
                idempotency_key TEXT PRIMARY KEY,
                command TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                job_id TEXT REFERENCES jobs(job_id),
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                thread_id TEXT,
                turn_id TEXT,
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL,
                completed_at REAL,
                stop_reason TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS service_lease (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                lease_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                released_at REAL
            )""",
            """CREATE TABLE IF NOT EXISTS notification_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                dedupe_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                occurred_at REAL NOT NULL,
                event_json TEXT NOT NULL,
                sink_kind TEXT NOT NULL,
                delivered_at REAL
            )""",
            """CREATE TABLE IF NOT EXISTS audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                occurred_at REAL NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                details_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE
            )""",
            "CREATE INDEX IF NOT EXISTS jobs_dispatch_idx ON jobs(state, priority DESC, created_at ASC)",
            "CREATE INDEX IF NOT EXISTS leases_state_idx ON leases(state, expires_at)",
            "CREATE INDEX IF NOT EXISTS proposals_state_idx ON job_proposals(state, priority DESC, created_at ASC)",
            "CREATE INDEX IF NOT EXISTS runs_state_idx ON runs(state, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS notification_delivery_idx ON notification_events(delivered_at, sequence)",
        ]
        with self.transaction() as connection:
            for statement in statements:
                connection.execute(statement)
            run_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "lease_expires_at" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0"
                )
            if "lease_owner" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN lease_owner TEXT")
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', '1')"
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES ('account_fingerprint_key', ?)",
                (secrets.token_hex(32),),
            )

    def bootstrap(self, *, policy: Dict[str, Any], now: float) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO controller(singleton, mode, reason_code, updated_at) VALUES (1, 'PAUSED', 'INITIALIZED_FAIL_CLOSED', ?)",
                (now,),
            )
            encoded = canonical_json(policy)
            connection.execute(
                """INSERT OR IGNORE INTO policies(singleton, version, policy_json, policy_hash, updated_at)
                   VALUES (1, 1, ?, ?, ?)""",
                (encoded, payload_hash(policy), now),
            )

    def execute_idempotent(
        self,
        *,
        key: str,
        command: str,
        request: Dict[str, Any],
        now: float,
        operation: Callable[[sqlite3.Connection], Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], bool]:
        if not key or len(key) > 200:
            raise SchedulerError("IDEMPOTENCY_REQUIRED", "A valid idempotency key is required")
        request_digest = payload_hash(request)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT command, request_hash, result_json FROM idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if row["command"] != command or row["request_hash"] != request_digest:
                    raise SchedulerError(
                        "IDEMPOTENCY_CONFLICT",
                        "The idempotency key was already used for another request",
                    )
                return json.loads(row["result_json"]), True
            result = operation(connection)
            connection.execute(
                "INSERT INTO idempotency(idempotency_key, command, request_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, command, request_digest, canonical_json(result), now),
            )
            return result, False

    def replay_idempotent(
        self,
        *,
        key: str,
        command: str,
        request: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Return a completed result before re-evaluating state-dependent input."""
        if not key or len(key) > 200:
            raise SchedulerError("IDEMPOTENCY_REQUIRED", "A valid idempotency key is required")
        request_digest = payload_hash(request)
        with self.reader() as connection:
            row = connection.execute(
                "SELECT command, request_hash, result_json FROM idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        if row["command"] != command or row["request_hash"] != request_digest:
            raise SchedulerError(
                "IDEMPOTENCY_CONFLICT",
                "The idempotency key was already used for another request",
            )
        return json.loads(row["result_json"])

    @staticmethod
    def record_idempotent(
        connection: sqlite3.Connection,
        *,
        key: str,
        command: str,
        request: Dict[str, Any],
        result: Dict[str, Any],
        now: float,
    ) -> None:
        if not key or len(key) > 200:
            raise SchedulerError("IDEMPOTENCY_REQUIRED", "A valid idempotency key is required")
        try:
            connection.execute(
                "INSERT INTO idempotency(idempotency_key, command, request_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (key, command, payload_hash(request), canonical_json(result), now),
            )
        except sqlite3.IntegrityError as exc:
            raise SchedulerError(
                "IDEMPOTENCY_CONFLICT",
                "The idempotency key completed concurrently",
                retryable=True,
            ) from exc

    @staticmethod
    def append_audit(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        actor: str,
        details: Dict[str, Any],
        now: float,
    ) -> str:
        previous = connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else "0" * 64
        event_id = new_id("evt")
        body = {
            "actor": actor,
            "details": details,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": now,
            "previous_hash": previous_hash,
        }
        event_hash = payload_hash(body)
        connection.execute(
            """INSERT INTO audit_events(
                   event_id, occurred_at, event_type, actor, details_json, previous_hash, event_hash
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                now,
                event_type,
                actor,
                canonical_json(details),
                previous_hash,
                event_hash,
            ),
        )
        return event_id

    def controller(self) -> Dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute("SELECT mode, reason_code, updated_at FROM controller WHERE singleton = 1").fetchone()
        if row is None:
            raise SchedulerError("STATE_INVALID", "The controller is not initialized")
        return dict(row)

    @staticmethod
    def set_controller(
        connection: sqlite3.Connection, *, mode: str, reason_code: Optional[str], now: float
    ) -> None:
        connection.execute(
            "UPDATE controller SET mode = ?, reason_code = ?, updated_at = ? WHERE singleton = 1",
            (mode, reason_code, now),
        )

    def policy(self) -> Dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT version, policy_json, policy_hash, updated_at FROM policies WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise SchedulerError("STATE_INVALID", "The policy is not initialized")
        return {
            "policy": json.loads(row["policy_json"]),
            "policy_hash": row["policy_hash"],
            "updated_at": row["updated_at"],
            "version": row["version"],
        }

    def latest_snapshot(self) -> Optional[Dict[str, Any]]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM quota_snapshots ORDER BY observed_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def account_fingerprint_key(self) -> str:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'account_fingerprint_key'"
            ).fetchone()
        if row is None:
            raise SchedulerError("STATE_INVALID", "The account fingerprint key is missing")
        return row["value"]

    def account_binding(self, profile_key: str) -> Optional[Dict[str, Any]]:
        with self.reader() as connection:
            return self.account_binding_in(connection, profile_key)

    @staticmethod
    def account_binding_in(
        connection: sqlite3.Connection, profile_key: str
    ) -> Optional[Dict[str, Any]]:
        row = connection.execute(
            """SELECT profile_key, account_fingerprint, account_type, plan_type,
                      first_observed_at, last_observed_at
               FROM account_bindings WHERE profile_key = ?""",
            (profile_key,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def latest_snapshot_in(connection: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        row = connection.execute(
            "SELECT snapshot_json FROM quota_snapshots ORDER BY observed_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                """SELECT job_id, work_ref, priority, state, manifest_json, approval_id, created_at, updated_at
                   FROM jobs ORDER BY priority DESC, created_at ASC, job_id ASC"""
            ).fetchall()
        return [
            {
                "approval_id": row["approval_id"],
                "created_at": row["created_at"],
                "job": json.loads(row["manifest_json"]),
                "job_id": row["job_id"],
                "priority": row["priority"],
                "state": row["state"],
                "updated_at": row["updated_at"],
                "work_ref": row["work_ref"],
            }
            for row in rows
        ]

    def get_job(self, job_id: str) -> Dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                "SELECT job_id, state, manifest_json, approval_id, created_at, updated_at FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise SchedulerError("JOB_NOT_FOUND", "The requested job does not exist")
        return {
            "approval_id": row["approval_id"],
            "created_at": row["created_at"],
            "job": json.loads(row["manifest_json"]),
            "job_id": row["job_id"],
            "state": row["state"],
            "updated_at": row["updated_at"],
        }

    def list_proposals(self) -> List[Dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                """SELECT job_id, work_ref, priority, state, package_hash,
                          created_at, updated_at, approved_at
                   FROM job_proposals
                   ORDER BY priority DESC, created_at ASC, job_id ASC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_proposal(self, job_id: str) -> Dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                """SELECT job_id, work_ref, priority, state, package_json, package_hash,
                          created_at, updated_at, approved_at
                   FROM job_proposals WHERE job_id = ?""",
                (job_id,),
            ).fetchone()
        if row is None:
            raise SchedulerError("PROPOSAL_NOT_FOUND", "The requested proposal does not exist")
        result = dict(row)
        result["package"] = json.loads(result.pop("package_json"))
        return result

    def list_runs(self) -> List[Dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                """SELECT run_id, job_id, kind, state, thread_id, turn_id, started_at,
                          updated_at, lease_expires_at, completed_at, stop_reason
                   FROM runs ORDER BY started_at DESC, run_id ASC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> Dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                """SELECT run_id, job_id, kind, state, thread_id, turn_id, started_at,
                          updated_at, lease_expires_at, completed_at, stop_reason
                   FROM runs WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
        if row is None:
            raise SchedulerError("RUN_NOT_FOUND", "The requested run does not exist")
        return dict(row)

    def acquire_service_lease(
        self,
        *,
        owner_id: str,
        now: float,
        lease_seconds: int,
    ) -> Dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT lease_id, owner_id, state, acquired_at, heartbeat_at,
                          expires_at, released_at
                   FROM service_lease WHERE singleton = 1"""
            ).fetchone()
            if (
                row is not None
                and row["state"] == "active"
                and row["expires_at"] > now
                and row["owner_id"] != owner_id
            ):
                raise SchedulerError(
                    "SERVICE_ALREADY_RUNNING",
                    "Another background supervisor owns the active service lease",
                    retryable=True,
                )
            if (
                row is not None
                and row["state"] == "active"
                and row["expires_at"] > now
                and row["owner_id"] == owner_id
            ):
                return {**dict(row), "recovered": False}
            recovered = row is not None and row["state"] == "active"
            lease_id = new_id("svc")
            expires_at = now + lease_seconds
            connection.execute(
                """INSERT INTO service_lease(
                       singleton, lease_id, owner_id, state, acquired_at,
                       heartbeat_at, expires_at, released_at
                   ) VALUES (1, ?, ?, 'active', ?, ?, ?, NULL)
                   ON CONFLICT(singleton) DO UPDATE SET
                       lease_id = excluded.lease_id,
                       owner_id = excluded.owner_id,
                       state = 'active',
                       acquired_at = excluded.acquired_at,
                       heartbeat_at = excluded.heartbeat_at,
                       expires_at = excluded.expires_at,
                       released_at = NULL""",
                (lease_id, owner_id, now, now, expires_at),
            )
        return {
            "acquired_at": now,
            "expires_at": expires_at,
            "heartbeat_at": now,
            "lease_id": lease_id,
            "owner_id": owner_id,
            "recovered": recovered,
            "released_at": None,
            "state": "active",
        }

    def renew_service_lease(
        self,
        *,
        lease_id: str,
        owner_id: str,
        now: float,
        lease_seconds: int,
    ) -> Dict[str, Any]:
        expires_at = now + lease_seconds
        with self.transaction() as connection:
            changed = connection.execute(
                """UPDATE service_lease
                   SET heartbeat_at = ?, expires_at = ?
                   WHERE singleton = 1 AND lease_id = ? AND owner_id = ?
                         AND state = 'active' AND expires_at > ?""",
                (now, expires_at, lease_id, owner_id, now),
            ).rowcount
            if changed != 1:
                raise SchedulerError(
                    "SERVICE_LEASE_LOST",
                    "The background supervisor lease is no longer valid",
                )
        return {"expires_at": expires_at, "heartbeat_at": now, "lease_id": lease_id}

    def release_service_lease(
        self,
        *,
        lease_id: str,
        owner_id: str,
        now: float,
    ) -> bool:
        with self.transaction() as connection:
            changed = connection.execute(
                """UPDATE service_lease
                   SET state = 'released', released_at = ?, heartbeat_at = ?, expires_at = ?
                   WHERE singleton = 1 AND lease_id = ? AND owner_id = ? AND state = 'active'""",
                (now, now, now, lease_id, owner_id),
            ).rowcount
        return changed == 1

    def service_lease_status(self, *, now: float) -> Dict[str, Any]:
        with self.reader() as connection:
            row = connection.execute(
                """SELECT lease_id, owner_id, state, acquired_at, heartbeat_at,
                          expires_at, released_at
                   FROM service_lease WHERE singleton = 1"""
            ).fetchone()
        if row is None:
            return {"present": False, "running": False}
        value = dict(row)
        return {
            "acquired_at": value["acquired_at"],
            "expires_at": value["expires_at"],
            "heartbeat_at": value["heartbeat_at"],
            "lease_id": value["lease_id"],
            "owner_prefix": value["owner_id"][:12],
            "present": True,
            "released_at": value["released_at"],
            "running": value["state"] == "active" and value["expires_at"] > now,
            "state": value["state"],
        }

    def reserve_notification(
        self,
        *,
        event: Dict[str, Any],
        dedupe_key: str,
        sink_kind: str,
    ) -> Dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT event_json, delivered_at FROM notification_events
                   WHERE dedupe_key = ?""",
                (dedupe_key,),
            ).fetchone()
            if row is not None:
                return {
                    "deliver": row["delivered_at"] is None,
                    "event": json.loads(row["event_json"]),
                    "replayed": True,
                }
            connection.execute(
                """INSERT INTO notification_events(
                       event_id, dedupe_key, event_type, occurred_at,
                       event_json, sink_kind, delivered_at
                   ) VALUES (?, ?, ?, ?, ?, ?, NULL)""",
                (
                    event["event_id"],
                    dedupe_key,
                    event["event_type"],
                    event["occurred_at"],
                    canonical_json(event),
                    sink_kind,
                ),
            )
        return {"deliver": True, "event": event, "replayed": False}

    def mark_notification_delivered(self, *, event_id: str, delivered_at: float) -> None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE notification_events SET delivered_at = ?
                   WHERE event_id = ? AND delivered_at IS NULL""",
                (delivered_at, event_id),
            )

    def list_notifications(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                """SELECT sequence, event_json, delivered_at, sink_kind
                   FROM notification_events ORDER BY sequence DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                **json.loads(row["event_json"]),
                "delivered_at": row["delivered_at"],
                "sequence": row["sequence"],
                "sink_kind": row["sink_kind"],
            }
            for row in rows
        ]

    def counts(self, *, now: float) -> Dict[str, Any]:
        with self.reader() as connection:
            job_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state ORDER BY state"
            ).fetchall()
            active_leases = connection.execute(
                "SELECT COUNT(*) AS count FROM leases WHERE state = 'active'"
            ).fetchone()["count"]
            stale_leases = connection.execute(
                "SELECT COUNT(*) AS count FROM leases WHERE state = 'active' AND expires_at <= ?",
                (now,),
            ).fetchone()["count"]
            proposal_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM job_proposals GROUP BY state ORDER BY state"
            ).fetchall()
            run_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM runs GROUP BY state ORDER BY state"
            ).fetchall()
        return {
            "active_leases": active_leases,
            "jobs": {row["state"]: row["count"] for row in job_rows},
            "proposals": {row["state"]: row["count"] for row in proposal_rows},
            "runs": {row["state"]: row["count"] for row in run_rows},
            "stale_leases": stale_leases,
        }

    def reconciliation_candidates(self, *, now: float) -> List[Dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                """SELECT lease_id, job_id, acquired_at, expires_at
                   FROM leases WHERE state = 'active' AND expires_at <= ?
                   ORDER BY expires_at ASC, lease_id ASC""",
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_reconciliation_candidates(self, *, now: float) -> List[Dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                """SELECT run_id, thread_id, turn_id, state, lease_expires_at
                   FROM runs
                   WHERE state IN ('starting', 'running') AND lease_expires_at <= ?
                   ORDER BY lease_expires_at ASC, run_id ASC""",
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_audit(self, *, limit: int) -> List[Dict[str, Any]]:
        with self.reader() as connection:
            rows = connection.execute(
                """SELECT sequence, event_id, occurred_at, event_type, actor, details_json,
                          previous_hash, event_hash
                   FROM audit_events ORDER BY sequence DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "actor": row["actor"],
                "details": json.loads(row["details_json"]),
                "event_hash": row["event_hash"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "previous_hash": row["previous_hash"],
                "sequence": row["sequence"],
            }
            for row in rows
        ]

    def verify_audit(self) -> Dict[str, Any]:
        with self.reader() as connection:
            return self.verify_audit_in(connection)

    @staticmethod
    def verify_audit_in(connection: sqlite3.Connection) -> Dict[str, Any]:
        rows = connection.execute(
            """SELECT sequence, event_id, occurred_at, event_type, actor, details_json,
                      previous_hash, event_hash
               FROM audit_events ORDER BY sequence ASC"""
        ).fetchall()
        expected_previous = "0" * 64
        for row in rows:
            if row["previous_hash"] != expected_previous:
                return {"event_count": len(rows), "first_invalid_sequence": row["sequence"], "valid": False}
            try:
                details = json.loads(row["details_json"])
            except (TypeError, ValueError):
                return {"event_count": len(rows), "first_invalid_sequence": row["sequence"], "valid": False}
            body = {
                "actor": row["actor"],
                "details": details,
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "previous_hash": row["previous_hash"],
            }
            if payload_hash(body) != row["event_hash"]:
                return {"event_count": len(rows), "first_invalid_sequence": row["sequence"], "valid": False}
            expected_previous = row["event_hash"]
        return {"event_count": len(rows), "first_invalid_sequence": None, "valid": True}

    def integrity(self) -> Dict[str, Any]:
        with self.reader() as connection:
            rows = connection.execute("PRAGMA quick_check").fetchall()
        messages = [row[0] for row in rows]
        return {"messages": messages if messages != ["ok"] else [], "valid": messages == ["ok"]}
