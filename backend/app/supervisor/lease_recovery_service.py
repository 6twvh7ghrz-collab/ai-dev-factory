"""V2.0-B2d: Lease Recovery Service — expired lease detection and safe reclamation.

Implements:
  - find_expired_assignments(): scan for assignments with expired leases
  - recover_assignment(): reclaim a single expired assignment
  - sweep_expired_assignments(): bulk expired assignment handler

Recovery rules:
  - CLAIMED tasks   → QUEUED (release claim back to queue)
  - RUNNING tasks   → BLOCKED (LEASE_EXPIRED_DURING_EXECUTION, no auto-retry)
  - assignment      → timeout (preserve history)
  - Worker BUSY     → AVAILABLE (only if no other active assignments)

Feature flag: V2_CONTROL_PLANE_ENABLED.
"""

import sqlite3
import uuid
import json
import hashlib
import os
from typing import Dict, Any, Optional, List
from datetime import datetime

from .worker_registry import (
    WORKER_STATUS_AVAILABLE,
    WORKER_STATUS_BUSY,
    ERROR_V2_CONTROL_PLANE_DISABLED,
    ERROR_WORKER_NOT_REGISTERED,
)


# ============================================================
# Error codes
# ============================================================

ERROR_ASSIGNMENT_NOT_FOUND   = "ASSIGNMENT_NOT_FOUND"
ERROR_STALE_LEASE            = "STALE_LEASE"
ERROR_IDEMPOTENCY_CONFLICT   = "IDEMPOTENCY_CONFLICT"
ERROR_VALIDATION_ERROR       = "VALIDATION_ERROR"
ERROR_INTERNAL_ERROR         = "INTERNAL_ERROR"
ERROR_TASK_SCOPE_VIOLATION   = "TASK_SCOPE_VIOLATION"
ERROR_RECOVERY_FAILED        = "RECOVERY_FAILED"


# ============================================================
# Active / terminal assignment statuses
# ============================================================

ACTIVE_ASSIGNMENT_STATUSES = frozenset([
    "assigned", "acknowledged", "running", "retrying",
])

TERMINAL_ASSIGNMENT_STATUSES = frozenset([
    "completed", "failed", "cancelled", "timeout",
])


class LeaseRecoveryService:
    """Expired lease scanner and safe reclamation engine.

    find_expired_assignments() scans for assignments whose lease has lapsed.
    recover_assignment() atomically transitions:
      - assignment.status → timeout
      - Task state reversion (CLAIMED→QUEUED or RUNNING→BLOCKED)
      - Worker release (BUSY→AVAILABLE when no remaining active assignments)
      - Append-only task_events ASSIGNMENT_LEASE_EXPIRED

    All mutations for a single recovery share one BEGIN IMMEDIATE transaction.
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
                None, None, None,
                ERROR_V2_CONTROL_PLANE_DISABLED,
                "V2_CONTROL_PLANE_ENABLED is off; operation rejected"
            )
        return None

    def _time_to_str(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    # ── Public API ──

    def find_expired_assignments(
        self,
        now: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Find assignments with expired leases that are still in active statuses.

        Only returns assignments in: assigned / acknowledged / running / retrying
        where lease_expires_at <= now.

        Args:
            now:   reference time (default: datetime.now())
            limit: max assignments to return

        Returns:
            List of dicts with: assignment_id, task_id, worker_id, status,
                                lease_expires_at, lease_token
            (lease_token is NOT returned — redacted for security)
        """
        now_str = self._time_to_str(now or datetime.now())

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT assignment_id, task_id, worker_id, status,
                       lease_expires_at, project_id
                FROM task_assignments
                WHERE status IN ('assigned','acknowledged','running','retrying')
                  AND lease_expires_at <= ?
                ORDER BY lease_expires_at ASC
                LIMIT ?
            """, (now_str, limit))
            rows = cur.fetchall()
            return [
                {
                    "assignment_id":       r["assignment_id"],
                    "task_id":             r["task_id"],
                    "worker_id":           r["worker_id"],
                    "status":              r["status"],
                    "lease_expires_at":    r["lease_expires_at"],
                    "project_id":          r["project_id"],
                }
                for r in rows
            ]
        except Exception as e:
            return []
        finally:
            conn.close()

    def sweep_expired_assignments(
        self,
        limit: int = 100,
        idempotency_prefix: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Find all expired assignments and attempt recovery for each.

        Each recovery uses a unique idempotency_key = {prefix}-{assignment_id}.
        Already-recovered (timeout) assignments are safely skipped via idempotency.

        Args:
            limit:               max expired assignments to scan
            idempotency_prefix:  prefix for per-assignment keys (default: "sweep")
            now:                 reference time

        Returns:
            List of per-assignment recovery results.
        """
        prefix = idempotency_prefix or "sweep"
        ref_time = now or datetime.now()

        expired = self.find_expired_assignments(now=ref_time, limit=limit)
        results = []
        for item in expired:
            aid = item["assignment_id"]
            idem_key = f"{prefix}-{aid}"
            # Reason with timestamp for audit traceability
            reason = (
                f"LEASE_EXPIRED: assignment {aid} lease expired at "
                f"{item['lease_expires_at']}"
            )
            r = self.recover_assignment(
                assignment_id=aid,
                reason=reason,
                idempotency_key=idem_key,
                now=ref_time,
            )
            results.append(r)
        return results

    def recover_assignment(
        self,
        assignment_id: str,
        reason: str,
        idempotency_key: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Recover a single expired assignment in an atomic transaction.

        Returns:
            {
                "success": bool,
                "assignment_id": str,
                "task_id": int or None,
                "worker_id": str or None,
                "previous_assignment_status": str or None,
                "assignment_status": str or None,
                "previous_task_state": str or None,
                "task_state": str or None,
                "worker_status": str or None,
                "state_version": int or None,
                "idempotent": bool,
                "error_code": str or None,
                "error_message": str or None,
            }
        """
        # ── Feature flag ──
        gate = self._v2_gate()
        if gate:
            return gate

        # ── Parameter validation ──
        if not isinstance(assignment_id, str) or not assignment_id.strip():
            return self._make_error(
                None, assignment_id, None,
                ERROR_VALIDATION_ERROR,
                "assignment_id must be a non-empty string"
            )
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return self._make_error(
                None, assignment_id, None,
                ERROR_VALIDATION_ERROR,
                "idempotency_key must be a non-empty string"
            )

        ref_time = now or datetime.now()
        now_str = self._time_to_str(ref_time)

        conn = self._get_conn()
        try:
            # ── 1. Idempotency check ──
            idem_result = self._check_idempotency(
                conn, assignment_id, idempotency_key, reason
            )
            if idem_result is not None:
                conn.close()
                return idem_result

            # ── 2. Load assignment ──
            assign = self._load_assignment(conn, assignment_id)
            if assign is None:
                conn.close()
                return self._make_error(
                    None, assignment_id, None,
                    ERROR_ASSIGNMENT_NOT_FOUND,
                    f"Assignment '{assignment_id}' not found"
                )

            # ── 3. Validate: must be active and expired ──
            status = assign["status"]
            if status not in ACTIVE_ASSIGNMENT_STATUSES:
                conn.close()
                return self._make_error(
                    assign["task_id"], assignment_id, assign["worker_id"],
                    ERROR_STALE_LEASE,
                    f"Assignment '{assignment_id}' status is '{status}', not active"
                )

            lease_exp = assign["lease_expires_at"]
            if lease_exp and lease_exp > now_str:
                conn.close()
                return self._make_error(
                    assign["task_id"], assignment_id, assign["worker_id"],
                    ERROR_STALE_LEASE,
                    f"Lease not yet expired (expires_at={lease_exp}, now={now_str})"
                )

            # ── 4. Re-check expiry inside transaction (BEGIN IMMEDIATE) ──
            result = self._execute_recovery_transaction(
                conn, assign, assignment_id, reason, idempotency_key,
                now_str, ref_time
            )
            conn.close()
            return result

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return self._make_error(
                None, assignment_id, None,
                ERROR_INTERNAL_ERROR, str(e)
            )

    # ── Idempotency ──

    def _build_recovery_fingerprint(
        self,
        assignment_id: str,
        task_id: int,
        worker_id: str,
        detected_assignment_status: str,
        detected_task_state: str,
        lease_expires_at: str,
        reason: str,
    ) -> str:
        """Build deterministic fingerprint for recovery idempotency."""
        data = json.dumps({
            "aid": assignment_id,
            "tid": task_id,
            "wid": worker_id,
            "das": detected_assignment_status,
            "dts": detected_task_state,
            "le": lease_expires_at,
            "r": reason,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()

    def _check_idempotency(
        self,
        conn: sqlite3.Connection,
        assignment_id: str,
        idempotency_key: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        """Check task_events for an existing idempotency_key.

        Uses the event's stored detail_json to rebuild the fingerprint from
        ORIGINAL state (not current DB state, which may have changed).

        Strategy: the event stores all original fingerprint inputs in detail_json.
        We rebuild the fingerprint from those stored values and compare against
        the stored _recovery_fingerprint for validation, then compute the new
        request's fingerprint from the same original inputs + the new reason.

        Returns None to proceed, or a cached/conflict dict.
        """
        cur = conn.cursor()
        cur.execute("""
            SELECT event_id, task_id, assignment_id, from_state, to_state,
                   detail_json, operator_id, operator_type,
                   state_version_before, state_version_after
            FROM task_events
            WHERE idempotency_key = ?
            LIMIT 1
        """, (idempotency_key,))
        row = cur.fetchone()
        if row is None:
            return None  # not seen

        # Extract stored metadata from event detail
        stored_fp = None
        detail = {}
        try:
            detail = json.loads(row["detail_json"] or "{}")
            stored_fp = detail.get("_recovery_fingerprint")
        except (json.JSONDecodeError, TypeError):
            pass

        # Extract ORIGINAL input values from the event (pre-recovery state)
        orig_assignment_status = detail.get("previous_assignment_status") or row["from_state"]
        orig_task_state = detail.get("previous_task_state") or row["from_state"]
        orig_lease_expires_at = detail.get("lease_expires_at") or ""
        orig_worker_id = detail.get("worker_id") or row["operator_id"]

        task_id = row["task_id"]
        worker_id = row["operator_id"]

        if stored_fp is not None:
            # Validate: rebuild from ORIGINAL inputs should match stored fingerprint
            rebuilt_fp = self._build_recovery_fingerprint(
                assignment_id, task_id, orig_worker_id,
                orig_assignment_status, orig_task_state,
                orig_lease_expires_at, detail.get("reason") or row["reason"]
            )
            if rebuilt_fp != stored_fp:
                # Stored fingerprint integrity failure — unexpected
                return self._make_error(
                    task_id, assignment_id, worker_id,
                    ERROR_INTERNAL_ERROR,
                    "Stored idempotency fingerprint mismatch"
                )

            # Build NEW request's fingerprint from original inputs + new reason
            new_fp = self._build_recovery_fingerprint(
                assignment_id, task_id, orig_worker_id,
                orig_assignment_status, orig_task_state,
                orig_lease_expires_at, reason
            )

            if new_fp == stored_fp:
                # Same request → idempotent return
                # Determine resulting task state from event's to_state and metadata
                recovery_action = detail.get("recovery_action", "")
                if recovery_action == "RELEASE_CLAIM":
                    result_task_state = "QUEUED"
                elif recovery_action == "BLOCK_EXECUTION":
                    result_task_state = "BLOCKED"
                else:
                    result_task_state = row["to_state"]

                return {
                    "success": True,
                    "assignment_id": assignment_id,
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "previous_assignment_status": orig_assignment_status,
                    "assignment_status": "timeout",
                    "previous_task_state": orig_task_state,
                    "task_state": result_task_state,
                    "worker_status": WORKER_STATUS_AVAILABLE,
                    "state_version": row["state_version_after"],
                    "idempotent": True,
                    "error_code": None,
                    "error_message": None,
                }
            else:
                return self._make_error(
                    task_id, assignment_id, worker_id,
                    ERROR_IDEMPOTENCY_CONFLICT,
                    f"Idempotency key '{idempotency_key}' already used with different parameters"
                )
        else:
            # Legacy: no fingerprint stored, treat as idempotent
            return {
                "success": True,
                "assignment_id": assignment_id,
                "task_id": task_id,
                "worker_id": worker_id,
                "previous_assignment_status": orig_assignment_status,
                "assignment_status": "timeout",
                "previous_task_state": orig_task_state,
                "task_state": row["to_state"],
                "worker_status": WORKER_STATUS_AVAILABLE,
                "state_version": row["state_version_after"] or 1,
                "idempotent": True,
                "error_code": None,
                "error_message": None,
            }

    # ── Assignment loading ──

    def _load_assignment(
        self, conn: sqlite3.Connection, assignment_id: str
    ) -> Optional[Dict[str, Any]]:
        """Load a single assignment by ID, including task state."""
        cur = conn.cursor()
        cur.execute("""
            SELECT a.assignment_id, a.task_id, a.worker_id, a.status,
                   a.lease_token, a.lease_expires_at, a.project_id,
                   t.status AS task_status, t.state_version
            FROM task_assignments a
            JOIN development_tasks t ON a.task_id = t.id
            WHERE a.assignment_id = ?
        """, (assignment_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    # ── Execute recovery (single atomic transaction) ──

    def _execute_recovery_transaction(
        self,
        conn: sqlite3.Connection,
        assign: Dict[str, Any],
        assignment_id: str,
        reason: str,
        idempotency_key: str,
        now_str: str,
        ref_time: datetime,
    ) -> Dict[str, Any]:
        """Execute the full recovery inside a single BEGIN IMMEDIATE transaction.

        Operations:
          1. Re-check assignment is still active and expired
          2. Determine recovery action from task state
          3. Update assignment.status → timeout
          4. Update task state (CLAIMED→QUEUED or RUNNING→BLOCKED)
          5. Write task_events (ASSIGNMENT_LEASE_EXPIRED)
          6. Release Worker (BUSY→AVAILABLE) if no other active assignments
        """
        cur = conn.cursor()

        task_id = assign["task_id"]
        worker_id = assign["worker_id"]
        project_id = assign["project_id"]
        previous_assignment_status = assign["status"]
        previous_task_state = (assign["task_status"] or "").upper()
        current_version = assign["state_version"] or 1
        lease_expires_at = assign["lease_expires_at"] or ""

        # ── Determine recovery action ──
        if previous_task_state == "CLAIMED":
            new_task_state = "QUEUED"
            recovery_action = "RELEASE_CLAIM"
        elif previous_task_state == "RUNNING":
            new_task_state = "BLOCKED"
            recovery_action = "BLOCK_EXECUTION"
        else:
            return self._make_error(
                task_id, assignment_id, worker_id,
                ERROR_STALE_LEASE,
                f"Task state '{previous_task_state}' not eligible for lease recovery"
            )

        # Build recovery fingerprint
        fingerprint = self._build_recovery_fingerprint(
            assignment_id, task_id, worker_id,
            previous_assignment_status, previous_task_state,
            lease_expires_at, reason
        )

        new_version = current_version + 1
        event_id = f"event-{uuid.uuid4().hex[:12]}"

        # Build event detail
        event_detail = {
            "reason": reason,
            "assignment_id": assignment_id,
            "worker_id": worker_id,
            "lease_expires_at": lease_expires_at,
            "recovery_action": recovery_action,
            "previous_task_state": previous_task_state,
            "resulting_task_state": new_task_state,
            "previous_assignment_status": previous_assignment_status,
            "_recovery_fingerprint": fingerprint,
        }
        event_detail_json = json.dumps(event_detail, ensure_ascii=False)

        try:
            conn.execute("BEGIN IMMEDIATE")

            # ── 1. Re-check assignment still active and expired within transaction ──
            cur.execute("""
                SELECT status, lease_expires_at FROM task_assignments
                WHERE assignment_id = ?
            """, (assignment_id,))
            recheck = cur.fetchone()
            if recheck is None:
                conn.rollback()
                return self._make_error(
                    task_id, assignment_id, worker_id,
                    ERROR_ASSIGNMENT_NOT_FOUND,
                    "Assignment vanished during recovery"
                )

            if recheck["status"] not in ACTIVE_ASSIGNMENT_STATUSES:
                conn.rollback()
                return self._make_error(
                    task_id, assignment_id, worker_id,
                    ERROR_STALE_LEASE,
                    f"Assignment already transitioned to '{recheck['status']}' "
                    f"(concurrent recovery)"
                )

            if recheck["lease_expires_at"] and recheck["lease_expires_at"] > now_str:
                conn.rollback()
                return self._make_error(
                    task_id, assignment_id, worker_id,
                    ERROR_STALE_LEASE,
                    "Lease no longer expired (concurrent heartbeat extended it)"
                )

            # ── 2. Update assignment.status → timeout ──
            cur.execute("""
                UPDATE task_assignments
                SET status = 'timeout', updated_at = ?
                WHERE assignment_id = ?
                  AND status IN ('assigned','acknowledged','running','retrying')
                  AND lease_expires_at <= ?
            """, (now_str, assignment_id, now_str))

            if cur.rowcount != 1:
                conn.rollback()
                return self._make_error(
                    task_id, assignment_id, worker_id,
                    ERROR_STALE_LEASE,
                    "Failed to mark assignment as timeout (concurrent change)"
                )

            # ── 3. Update task state with optimistic version lock ──
            #     Use lowercase for SQLite storage
            new_status_lower = new_task_state.lower()
            cur.execute("""
                UPDATE development_tasks
                SET status = ?, state_version = ?, last_state_change = ?
                WHERE id = ? AND state_version = ?
            """, (new_status_lower, new_version, now_str, task_id, current_version))

            if cur.rowcount != 1:
                conn.rollback()
                return self._make_error(
                    task_id, assignment_id, worker_id,
                    ERROR_RECOVERY_FAILED,
                    "Task state version conflict (concurrent update)"
                )

            # ── 4. Write ASSIGNMENT_LEASE_EXPIRED event ──
            cur.execute("""
                INSERT INTO task_events
                (event_id, task_id, assignment_id, project_id, event_type,
                 from_state, to_state, reason, detail_json,
                 operator_type, operator_id, idempotency_key,
                 state_version_before, state_version_after, created_at)
                VALUES (?, ?, ?, ?, 'lease_expired',
                        ?, ?, ?, ?,
                        'system', 'system', ?,
                        ?, ?, ?)
            """, (
                event_id, task_id, assignment_id, project_id,
                previous_assignment_status, "timeout",
                reason, event_detail_json,
                idempotency_key,
                current_version, new_version, now_str,
            ))

            # ── 5. Release Worker: BUSY → AVAILABLE (only if no other active assignments) ──
            worker_status_result = self._safe_release_worker(
                conn, cur, worker_id, now_str
            )

            conn.commit()

            return {
                "success": True,
                "assignment_id": assignment_id,
                "task_id": task_id,
                "worker_id": worker_id,
                "previous_assignment_status": previous_assignment_status,
                "assignment_status": "timeout",
                "previous_task_state": previous_task_state,
                "task_state": new_task_state,
                "worker_status": worker_status_result,
                "state_version": new_version,
                "idempotent": False,
                "error_code": None,
                "error_message": None,
            }

        except Exception:
            conn.rollback()
            raise

    # ── Worker release ──

    def _safe_release_worker(
        self,
        conn: sqlite3.Connection,
        cur: sqlite3.Cursor,
        worker_id: str,
        now_str: str,
    ) -> str:
        """Release worker BUSY→AVAILABLE only if no other active assignments remain.

        Returns the worker's resulting status string.
        """
        # Count remaining active (non-expired) assignments for this worker
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM task_assignments
            WHERE worker_id = ?
              AND status IN ('assigned','acknowledged','running','retrying')
              AND lease_expires_at > ?
        """, (worker_id, now_str))
        count_row = cur.fetchone()
        remaining_active = count_row["cnt"] if count_row else 0

        if remaining_active == 0:
            # No other active assignments → safe to release
            cur.execute("""
                UPDATE agent_workers
                SET status = ?, last_seen_at = ?, version = version + 1
                WHERE worker_id = ? AND status = ?
            """, (WORKER_STATUS_AVAILABLE, now_str, worker_id, WORKER_STATUS_BUSY))
            return WORKER_STATUS_AVAILABLE
        else:
            # Worker still has other active assignments → keep BUSY
            return WORKER_STATUS_BUSY

    # ── Result builders ──

    @staticmethod
    def _make_error(
        task_id: Optional[int],
        assignment_id: str,
        worker_id: Optional[str],
        error_code: str,
        error_message: str,
    ) -> Dict[str, Any]:
        return {
            "success": False,
            "assignment_id": assignment_id,
            "task_id": task_id,
            "worker_id": worker_id,
            "previous_assignment_status": None,
            "assignment_status": None,
            "previous_task_state": None,
            "task_state": None,
            "worker_status": None,
            "state_version": None,
            "idempotent": False,
            "error_code": error_code,
            "error_message": error_message,
        }
