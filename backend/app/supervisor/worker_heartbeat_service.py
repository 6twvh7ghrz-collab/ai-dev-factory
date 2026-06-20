"""V2.0-B2c: Worker Heartbeat Service — lease renewal for active assignments.

Implements heartbeat(): renew the lease on an active task assignment,
with worker validation, lease token verification, and idempotency.

Feature flag: V2_CONTROL_PLANE_ENABLED.
"""

import sqlite3
import uuid
import json
import hashlib
import os
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from .worker_registry import (
    WORKER_STATUS_OFFLINE,
    WORKER_STATUS_DISABLED,
    ERROR_V2_CONTROL_PLANE_DISABLED,
    ERROR_WORKER_NOT_REGISTERED,
)


# ============================================================
# Error codes
# ============================================================

ERROR_WORKER_NOT_AVAILABLE   = "WORKER_NOT_AVAILABLE"
ERROR_ASSIGNMENT_NOT_FOUND   = "ASSIGNMENT_NOT_FOUND"
ERROR_TASK_SCOPE_VIOLATION   = "TASK_SCOPE_VIOLATION"
ERROR_LEASE_CONFLICT         = "LEASE_CONFLICT"
ERROR_STALE_LEASE            = "STALE_LEASE"
ERROR_IDEMPOTENCY_CONFLICT   = "IDEMPOTENCY_CONFLICT"
ERROR_VALIDATION_ERROR       = "VALIDATION_ERROR"
ERROR_INTERNAL_ERROR         = "INTERNAL_ERROR"


# ============================================================
# Active assignment statuses (eligible for heartbeat)
# ============================================================

ACTIVE_ASSIGNMENT_STATUSES = frozenset([
    "assigned", "acknowledged", "running", "retrying",
])

# Terminal assignment statuses (heartbeat rejected)
TERMINAL_ASSIGNMENT_STATUSES = frozenset([
    "completed", "failed", "cancelled", "timeout",
])


# ============================================================
# Lease limits
# ============================================================

LEASE_SECONDS_MIN = 30
LEASE_SECONDS_MAX = 3600


class WorkerHeartbeatService:
    """Renew lease on active task assignments with full validation chain.

    Transaction order:
      1. INSERT agent_heartbeats
      2. UPDATE task_assignments.lease_expires_at
      3. UPDATE agent_workers.last_seen_at

    All three writes share a single BEGIN IMMEDIATE transaction.
    Heartbeat does NOT change Task status or state_version.
    """

    def __init__(self, db_path: str, v2_enabled: Optional[bool] = None):
        self.db_path = db_path
        if v2_enabled is not None:
            self._v2_enabled = v2_enabled
        else:
            val = os.getenv("V2_CONTROL_PLANE_ENABLED", "false").lower()
            self._v2_enabled = val in ("true", "1")

    @property
    def is_v2_enabled(self) -> bool:
        return self._v2_enabled

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _v2_gate(self) -> Optional[Dict[str, Any]]:
        if not self._v2_enabled:
            return self._make_error(
                None, None, None, ERROR_V2_CONTROL_PLANE_DISABLED,
                "V2_CONTROL_PLANE_ENABLED is off; operation rejected"
            )
        return None

    # ── Public API ──

    def heartbeat(
        self,
        task_id: int,
        assignment_id: str,
        worker_id: str,
        lease_token: str,
        idempotency_key: str,
        extend_seconds: int = 300,
    ) -> Dict[str, Any]:
        """Renew the lease on an active assignment.

        Returns:
            {
                "success": bool,
                "heartbeat_id": str or None,
                "assignment_id": str or None,
                "task_id": int,
                "worker_id": str or None,
                "previous_expires_at": str or None,
                "lease_expires_at": str or None,
                "worker_last_seen_at": str or None,
                "idempotent": bool,
                "error_code": str or None,
                "error_message": str or None,
            }
        """
        # ── Feature flag ──
        gate = self._v2_gate()
        if gate:
            return gate

        # ── Validation ──
        val_err = self._validate_params(
            task_id, assignment_id, worker_id, lease_token,
            idempotency_key, extend_seconds
        )
        if val_err:
            return val_err

        # Build fingerprint for idempotency
        fingerprint = self._build_heartbeat_fingerprint(
            task_id, assignment_id, worker_id, lease_token, extend_seconds
        )

        conn = self._get_conn()
        try:
            # ── 1. Idempotency check ──
            idem_result = self._check_idempotency(
                conn, idempotency_key, fingerprint,
                task_id, assignment_id, worker_id
            )
            if idem_result is not None:
                conn.close()
                return idem_result

            # ── 2. Worker validation ──
            worker_err = self._validate_worker(conn, worker_id)
            if worker_err:
                conn.close()
                return worker_err

            # ── 3. Assignment validation ──
            assign_err = self._validate_assignment(
                conn, task_id, assignment_id, worker_id, lease_token
            )
            if assign_err is not None:
                conn.close()
                return assign_err

            # ── 4. Lease expiration check ──
            stale_err = self._check_lease_not_expired(
                conn, assignment_id
            )
            if stale_err:
                conn.close()
                return stale_err

            # ── 5. Execute heartbeat transaction ──
            result = self._execute_heartbeat(
                conn, task_id, assignment_id, worker_id,
                idempotency_key, extend_seconds, fingerprint
            )
            conn.close()
            return result

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_INTERNAL_ERROR, str(e)
            )

    # ── Param validation ──

    def _validate_params(
        self,
        task_id: int,
        assignment_id: str,
        worker_id: str,
        lease_token: str,
        idempotency_key: str,
        extend_seconds: int,
    ) -> Optional[Dict[str, Any]]:
        """Validate all scalar parameters before touching the DB."""

        # task_id
        if not isinstance(task_id, int) or task_id <= 0:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_VALIDATION_ERROR,
                "task_id must be a positive integer"
            )

        # assignment_id
        if not isinstance(assignment_id, str) or not assignment_id.strip():
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_VALIDATION_ERROR,
                "assignment_id must be a non-empty string"
            )

        # worker_id
        if not isinstance(worker_id, str) or not worker_id.strip():
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_VALIDATION_ERROR,
                "worker_id must be a non-empty string"
            )

        # lease_token
        if not isinstance(lease_token, str) or not lease_token.strip():
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_VALIDATION_ERROR,
                "lease_token must be a non-empty string"
            )

        # idempotency_key
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_VALIDATION_ERROR,
                "idempotency_key must be a non-empty string"
            )

        # extend_seconds
        if not isinstance(extend_seconds, int) or extend_seconds < LEASE_SECONDS_MIN or extend_seconds > LEASE_SECONDS_MAX:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_VALIDATION_ERROR,
                f"extend_seconds must be between {LEASE_SECONDS_MIN} and {LEASE_SECONDS_MAX}, got {extend_seconds}"
            )

        return None  # valid

    # ── Worker validation ──

    def _validate_worker(
        self, conn: sqlite3.Connection, worker_id: str
    ) -> Optional[Dict[str, Any]]:
        """Check worker exists and is not OFFLINE / DISABLED."""
        cur = conn.cursor()
        cur.execute("""
            SELECT worker_id, status, last_seen_at
            FROM agent_workers WHERE worker_id = ?
        """, (worker_id,))
        row = cur.fetchone()
        if row is None:
            return self._make_error(
                None, None, worker_id,
                ERROR_WORKER_NOT_REGISTERED,
                f"Worker '{worker_id}' not registered"
            )

        status = row["status"]
        if status == WORKER_STATUS_OFFLINE:
            return self._make_error(
                None, None, worker_id,
                ERROR_WORKER_NOT_AVAILABLE,
                f"Worker '{worker_id}' is OFFLINE"
            )
        if status == WORKER_STATUS_DISABLED:
            return self._make_error(
                None, None, worker_id,
                ERROR_WORKER_NOT_AVAILABLE,
                f"Worker '{worker_id}' is DISABLED"
            )

        return None  # valid

    # ── Assignment validation ──

    def _validate_assignment(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        assignment_id: str,
        worker_id: str,
        lease_token: str,
    ) -> Optional[Dict[str, Any]]:
        """Verify assignment exists with matching task/worker/lease_token."""
        cur = conn.cursor()
        cur.execute("""
            SELECT assignment_id, task_id, worker_id, lease_token,
                   lease_expires_at, status
            FROM task_assignments
            WHERE assignment_id = ?
        """, (assignment_id,))
        row = cur.fetchone()

        if row is None:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_ASSIGNMENT_NOT_FOUND,
                f"Assignment '{assignment_id}' not found"
            )

        # task_id must match
        if row["task_id"] != task_id:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_TASK_SCOPE_VIOLATION,
                f"task_id mismatch: expected {task_id}, assignment has {row['task_id']}"
            )

        # worker_id must match
        if row["worker_id"] != worker_id:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_LEASE_CONFLICT,
                f"Worker mismatch: expected '{worker_id}', assignment owned by '{row['worker_id']}'"
            )

        # lease_token must match
        if row["lease_token"] != lease_token:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_LEASE_CONFLICT,
                "lease_token does not match assignment"
            )

        # Status must be active
        status = row["status"]
        if status in TERMINAL_ASSIGNMENT_STATUSES:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_STALE_LEASE,
                f"Cannot heartbeat assignment with status '{status}'"
            )

        if status not in ACTIVE_ASSIGNMENT_STATUSES:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_STALE_LEASE,
                f"Unknown assignment status '{status}'"
            )

        return None  # valid

    # ── Lease expiration check ──

    def _check_lease_not_expired(
        self, conn: sqlite3.Connection, assignment_id: str
    ) -> Optional[Dict[str, Any]]:
        """Check that the current lease is not expired.

        Lease is considered expired when lease_expires_at <= now.
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.cursor()
        cur.execute("""
            SELECT lease_expires_at, task_id, worker_id
            FROM task_assignments
            WHERE assignment_id = ?
              AND lease_expires_at > ?
        """, (assignment_id, now_str))
        row = cur.fetchone()
        if row is None:
            # Read the current expires_at for the error message
            cur.execute("""
                SELECT task_id, worker_id, lease_expires_at
                FROM task_assignments WHERE assignment_id = ?
            """, (assignment_id,))
            info = cur.fetchone()
            tid = info["task_id"] if info else None
            wid = info["worker_id"] if info else None
            prev = info["lease_expires_at"] if info else "unknown"
            return self._make_error(
                tid, assignment_id, wid,
                ERROR_STALE_LEASE,
                f"Lease already expired (expires_at={prev}, now={now_str})"
            )
        return None  # valid (lease is still active)

    # ── Idempotency ──

    def _build_heartbeat_fingerprint(
        self,
        task_id: int,
        assignment_id: str,
        worker_id: str,
        lease_token: str,
        extend_seconds: int,
    ) -> str:
        """Build fingerprint from request params (NOT including plaintext token)."""
        token_hash = hashlib.sha256(lease_token.encode()).hexdigest()
        data = json.dumps({
            "tid": task_id,
            "aid": assignment_id,
            "wid": worker_id,
            "th": token_hash,
            "es": extend_seconds,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()

    def _check_idempotency(
        self,
        conn: sqlite3.Connection,
        idempotency_key: str,
        fingerprint: str,
        task_id: int,
        assignment_id: str,
        worker_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Check agent_heartbeats table for an existing idempotency_key entry.

        The fingerprint is stored in the lease_token column of agent_heartbeats
        (the real lease_token is redacted to ***REDACTED*** in heartbeat records).

        Returns:
            None → proceed with heartbeat
            Dict → cached result (same fingerprint) or conflict (different fingerprint)
        """
        cur = conn.cursor()
        cur.execute("""
            SELECT heartbeat_id, task_id, assignment_id, worker_id,
                   lease_token AS stored_fingerprint, renewed_at
            FROM agent_heartbeats
            WHERE idempotency_key = ?
            LIMIT 1
        """, (idempotency_key,))
        row = cur.fetchone()

        if row is None:
            return None  # not seen, proceed

        stored_fp = row["stored_fingerprint"]

        if stored_fp == fingerprint:
            # Same fingerprint → idempotent, return cached result
            cur.execute("""
                SELECT lease_expires_at FROM task_assignments
                WHERE assignment_id = ?
            """, (assignment_id,))
            assign_row = cur.fetchone()
            lease_expires_at = assign_row["lease_expires_at"] if assign_row else None

            cur.execute("""
                SELECT last_seen_at FROM agent_workers WHERE worker_id = ?
            """, (worker_id,))
            worker_row = cur.fetchone()
            worker_last_seen = worker_row["last_seen_at"] if worker_row else None

            return {
                "success": True,
                "heartbeat_id": row["heartbeat_id"],
                "assignment_id": assignment_id,
                "task_id": task_id,
                "worker_id": worker_id,
                "previous_expires_at": lease_expires_at,
                "lease_expires_at": lease_expires_at,
                "worker_last_seen_at": worker_last_seen,
                "idempotent": True,
                "error_code": None,
                "error_message": None,
            }
        else:
            # Same key, different fingerprint → conflict
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_IDEMPOTENCY_CONFLICT,
                f"Idempotency key '{idempotency_key}' already used with different parameters"
            )

    # ── Execute heartbeat (single atomic transaction) ──

    def _execute_heartbeat(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        assignment_id: str,
        worker_id: str,
        idempotency_key: str,
        extend_seconds: int,
        fingerprint: str,
    ) -> Dict[str, Any]:
        """Execute heartbeat inside a single BEGIN IMMEDIATE transaction.

        Operations:
          1. INSERT agent_heartbeats
          2. UPDATE task_assignments.lease_expires_at
          3. UPDATE agent_workers.last_seen_at
        """

        heartbeat_id = f"hb-{uuid.uuid4().hex[:12]}"
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        new_expires_at = (now + timedelta(seconds=extend_seconds)).strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.cursor()

        # Read current values before transaction
        cur.execute("""
            SELECT lease_expires_at FROM task_assignments
            WHERE assignment_id = ?
        """, (assignment_id,))
        assign_row = cur.fetchone()
        previous_expires_at = assign_row["lease_expires_at"] if assign_row else None

        try:
            conn.execute("BEGIN IMMEDIATE")

            # 1. INSERT agent_heartbeats (fingerprint stored as lease_token for idempotency)
            cur.execute("""
                INSERT INTO agent_heartbeats
                (heartbeat_id, worker_id, task_id, assignment_id,
                 lease_token, idempotency_key, renewed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                heartbeat_id, worker_id, task_id, assignment_id,
                fingerprint, idempotency_key, now_str, now_str,
            ))

            # 2. UPDATE task_assignments.lease_expires_at
            #    Only update if lease is not already expired (re-check in transaction)
            cur.execute("""
                UPDATE task_assignments
                SET lease_expires_at = ?, updated_at = ?
                WHERE assignment_id = ?
                  AND status IN ('assigned','acknowledged','running','retrying')
                  AND lease_expires_at > ?
            """, (new_expires_at, now_str, assignment_id, now_str))

            if cur.rowcount != 1:
                conn.rollback()
                return self._make_error(
                    task_id, assignment_id, worker_id,
                    ERROR_STALE_LEASE,
                    "Lease expired during transaction (concurrent timeout)"
                )

            # 3. UPDATE agent_workers.last_seen_at
            cur.execute("""
                UPDATE agent_workers
                SET last_seen_at = ?
                WHERE worker_id = ?
            """, (now_str, worker_id))

            conn.commit()

            return {
                "success": True,
                "heartbeat_id": heartbeat_id,
                "assignment_id": assignment_id,
                "task_id": task_id,
                "worker_id": worker_id,
                "previous_expires_at": previous_expires_at,
                "lease_expires_at": new_expires_at,
                "worker_last_seen_at": now_str,
                "idempotent": False,
                "error_code": None,
                "error_message": None,
            }

        except Exception:
            conn.rollback()
            raise

    # ── Result builders ──

    @staticmethod
    def _make_error(
        task_id: Optional[int],
        assignment_id: Optional[str],
        worker_id: Optional[str],
        error_code: str,
        error_message: str,
    ) -> Dict[str, Any]:
        return {
            "success": False,
            "heartbeat_id": None,
            "assignment_id": assignment_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "previous_expires_at": None,
            "lease_expires_at": None,
            "worker_last_seen_at": None,
            "idempotent": False,
            "error_code": error_code,
            "error_message": error_message,
        }
