"""V2 Task State Machine — 14-state transition engine with role-based access.

Defines the complete task lifecycle:
DRAFT → PLANNED → APPROVED → QUEUED → CLAIMED → RUNNING →
RESULT_SUBMITTED → REVIEWING → VERIFIED | REWORK | BLOCKED | NEED_USER
                                                                  |
                                                           FAILED / CANCELLED

Role-based guardrails:
  - Worker:   can only reach RESULT_SUBMITTED (from RUNNING)
  - Reviewer: can write VERIFIED / REWORK / BLOCKED / NEED_USER
  - Supervisor: can write PLANNED / APPROVED / QUEUED
  - System:   can write FAILED / CANCELLED (terminal)
  - User:     can approve from APPROVED through NEED_USER decisions

Optimistic locking via expected_version (WHERE state_version = ?).
Idempotency via idempotency_key — full fingerprint comparison.
Feature flag gating: V2_CONTROL_PLANE_ENABLED must be True.
"""
import sqlite3
import uuid
import json
import hashlib
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime
from pathlib import Path

from .task_event_service import TaskEventService

# ============================================================
# State definitions
# ============================================================

STATE_DRAFT = "DRAFT"
STATE_PLANNED = "PLANNED"
STATE_APPROVED = "APPROVED"
STATE_QUEUED = "QUEUED"
STATE_CLAIMED = "CLAIMED"
STATE_RUNNING = "RUNNING"
STATE_RESULT_SUBMITTED = "RESULT_SUBMITTED"
STATE_REVIEWING = "REVIEWING"
STATE_VERIFIED = "VERIFIED"
STATE_REWORK = "REWORK"
STATE_BLOCKED = "BLOCKED"
STATE_NEED_USER = "NEED_USER"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"

ALL_STATES = frozenset([
    STATE_DRAFT,
    STATE_PLANNED,
    STATE_APPROVED,
    STATE_QUEUED,
    STATE_CLAIMED,
    STATE_RUNNING,
    STATE_RESULT_SUBMITTED,
    STATE_REVIEWING,
    STATE_VERIFIED,
    STATE_REWORK,
    STATE_BLOCKED,
    STATE_NEED_USER,
    STATE_FAILED,
    STATE_CANCELLED,
])

TERMINAL_STATES = frozenset([
    STATE_VERIFIED,
    STATE_FAILED,
    STATE_CANCELLED,
])

# ============================================================
# Actor types
# ============================================================

ACTOR_SYSTEM = "system"
ACTOR_SUPERVISOR = "supervisor"
ACTOR_WORKER = "worker"
ACTOR_REVIEWER = "reviewer"
ACTOR_USER = "user"

ALL_ACTORS = frozenset([
    ACTOR_SYSTEM, ACTOR_SUPERVISOR, ACTOR_WORKER, ACTOR_REVIEWER, ACTOR_USER,
])

# ============================================================
# Role permission matrix
# ============================================================

# Which states each actor can WRITE (not which transitions they can trigger)
ACTOR_WRITEABLE_STATES: Dict[str, frozenset] = {
    ACTOR_SYSTEM:     frozenset([STATE_FAILED, STATE_CANCELLED, STATE_BLOCKED]),
    ACTOR_SUPERVISOR: frozenset([STATE_PLANNED, STATE_APPROVED, STATE_QUEUED,
                                  STATE_CLAIMED, STATE_RUNNING, STATE_REVIEWING]),
    ACTOR_WORKER:     frozenset([STATE_RESULT_SUBMITTED]),
    ACTOR_REVIEWER:   frozenset([STATE_VERIFIED, STATE_REWORK, STATE_BLOCKED, STATE_NEED_USER]),
    ACTOR_USER:       frozenset([STATE_APPROVED, STATE_CANCELLED, STATE_NEED_USER]),
}

# ============================================================
# Transition rules: {from_state: frozenset(valid to_states)}
# ============================================================

TRANSITION_GRAPH: Dict[str, frozenset] = {
    STATE_DRAFT: frozenset([
        STATE_PLANNED,
        STATE_CANCELLED,
    ]),
    STATE_PLANNED: frozenset([
        STATE_APPROVED,
        STATE_DRAFT,  # re-plan
        STATE_CANCELLED,
    ]),
    STATE_APPROVED: frozenset([
        STATE_QUEUED,
        STATE_CANCELLED,
    ]),
    STATE_QUEUED: frozenset([
        STATE_CLAIMED,
        STATE_CANCELLED,
    ]),
    STATE_CLAIMED: frozenset([
        STATE_RUNNING,
        STATE_QUEUED,  # release claim
        STATE_CANCELLED,
    ]),
    STATE_RUNNING: frozenset([
        STATE_RESULT_SUBMITTED,
        STATE_FAILED,
        STATE_CANCELLED,
        STATE_BLOCKED,    # lease-expiry recovery: system-initiated block
    ]),
    STATE_RESULT_SUBMITTED: frozenset([
        STATE_REVIEWING,
        STATE_CANCELLED,
    ]),
    STATE_REVIEWING: frozenset([
        STATE_VERIFIED,
        STATE_REWORK,
        STATE_BLOCKED,
        STATE_NEED_USER,
        STATE_CANCELLED,
    ]),
    STATE_REWORK: frozenset([
        STATE_QUEUED,   # back to queue for re-execution
        STATE_CANCELLED,
    ]),
    STATE_BLOCKED: frozenset([
        STATE_QUEUED,   # unblock → re-dispatch
        STATE_CANCELLED,
    ]),
    STATE_NEED_USER: frozenset([
        STATE_APPROVED,
        STATE_CANCELLED,
        STATE_REWORK,   # user decides to redo
    ]),
    STATE_VERIFIED: frozenset([]),   # terminal
    STATE_FAILED: frozenset([
        STATE_QUEUED,    # retry
        STATE_CANCELLED,
    ]),
    STATE_CANCELLED: frozenset([]),  # terminal
}


# ============================================================
# Error codes
# ============================================================

ERROR_INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
ERROR_STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
ERROR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERROR_ACTOR_NOT_AUTHORIZED = "ACTOR_NOT_AUTHORIZED"
ERROR_TERMINAL_STATE = "TERMINAL_STATE"
ERROR_TASK_NOT_FOUND = "TASK_NOT_FOUND"
ERROR_INVALID_ACTOR = "INVALID_ACTOR"
ERROR_V2_CONTROL_PLANE_DISABLED = "V2_CONTROL_PLANE_DISABLED"


class TaskStateMachineService:
    """14-state task lifecycle engine with optimistic locking, role guards,
    idempotency, and feature flag gating.

    WARNING: transition() is gated by V2_CONTROL_PLANE_ENABLED.
    When disabled, all mutation requests return V2_CONTROL_PLANE_DISABLED.
    """

    def __init__(self, db_path: str, v2_enabled: Optional[bool] = None):
        """Initialize state machine with database path.

        Args:
            db_path: path to SQLite database file
            v2_enabled: override V2_CONTROL_PLANE_ENABLED (None = read from env/settings)
        """
        self.db_path = db_path
        self.event_service = TaskEventService(db_path)
        if v2_enabled is not None:
            self._v2_enabled = v2_enabled
        else:
            # Read from settings to avoid import-time side effects
            import os
            val = os.getenv("V2_CONTROL_PLANE_ENABLED", "false").lower()
            self._v2_enabled = val in ("true", "1")

    @property
    def is_v2_enabled(self) -> bool:
        return self._v2_enabled

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new SQLite connection with foreign keys enabled."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    # ── Query ──

    def get_current_state(self, task_id: int) -> Dict[str, Any]:
        """Get current state and version of a task.

        Returns:
            {"success": bool, "state": str, "state_version": int, "error": str}
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT status, state_version FROM development_tasks WHERE id = ?",
                (task_id,)
            )
            row = cur.fetchone()
            if row is None:
                return {"success": False, "state": None, "state_version": None,
                        "error": ERROR_TASK_NOT_FOUND}
            return {"success": True, "state": row["status"].upper() if row["status"] else "DRAFT",
                    "state_version": row["state_version"] or 1, "error": None}
        except Exception as e:
            return {"success": False, "state": None, "state_version": None, "error": str(e)}
        finally:
            conn.close()

    # ── Authorization ──

    def can_transition(self, task_id: int, target_state: str, actor_type: str) -> Dict[str, Any]:
        """Check if an actor can transition a task to a target state.

        Returns:
            {"allowed": bool, "reason": str, "current_state": str, "current_version": int}
        """
        # Validate input
        if target_state not in ALL_STATES:
            return {"allowed": False, "reason": f"unknown target state: {target_state}",
                    "current_state": None, "current_version": None}
        if actor_type not in ALL_ACTORS:
            return {"allowed": False, "reason": f"unknown actor type: {actor_type}",
                    "current_state": None, "current_version": None}

        # Get current state
        result = self.get_current_state(task_id)
        if not result["success"]:
            return {"allowed": False, "reason": result["error"],
                    "current_state": None, "current_version": None}

        current_state = result["state"]
        current_version = result["state_version"]

        # Check: is current state terminal?
        if current_state in TERMINAL_STATES:
            return {"allowed": False, "reason": ERROR_TERMINAL_STATE,
                    "current_state": current_state, "current_version": current_version}

        # Check: is target state in transition graph?
        valid_targets = TRANSITION_GRAPH.get(current_state, frozenset())
        if target_state not in valid_targets:
            return {"allowed": False, "reason": ERROR_INVALID_STATE_TRANSITION,
                    "current_state": current_state, "current_version": current_version}

        # Check: is actor authorized to write target state?
        writable = ACTOR_WRITEABLE_STATES.get(actor_type, frozenset())
        if target_state not in writable:
            return {"allowed": False, "reason": ERROR_ACTOR_NOT_AUTHORIZED,
                    "current_state": current_state, "current_version": current_version}

        return {"allowed": True, "reason": "ok",
                "current_state": current_state, "current_version": current_version}

    # ── Transition (core API) ──

    def transition(
        self,
        task_id: int,
        target_state: str,
        actor_type: str,
        reason: str = "",
        expected_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a state transition atomically with optimistic locking.

        This is THE single write path for task state changes. Every transition
        is verified for:
          1. Valid state transition in TRANSITION_GRAPH
          2. Actor authorization in ACTOR_WRITEABLE_STATES
          3. Optimistic version check (WHERE state_version = ?)
          4. Idempotency check (same key = cached result)

        On success, writes a task_events record inside the same transaction
        and increments state_version.

        Args:
            task_id:         development_tasks.id
            target_state:    one of the 14 STATE_* constants
            actor_type:      system / supervisor / worker / reviewer / user
            reason:          human-readable transition reason
            expected_version: optimistic lock version (optional, 1 if null)
            idempotency_key: client-supplied unique key for idempotency
            metadata:        optional JSON-serializable dict for event detail

        Returns:
            {
                "success": bool,
                "task_id": int,
                "from_state": str, "to_state": str,
                "new_version": int,
                "event_id": str or None,
                "error": str or None,
                "error_code": str or None,
                "idempotent": bool,
            }
        """
        # ── Feature flag gate ──
        if not self._v2_enabled:
            return self._error(task_id, None, target_state, ERROR_V2_CONTROL_PLANE_DISABLED,
                              "V2_CONTROL_PLANE_ENABLED is off; transition rejected")

        # ── Validate inputs ──
        if target_state not in ALL_STATES:
            return self._error(task_id, None, target_state, ERROR_INVALID_STATE_TRANSITION,
                              f"unknown target state: {target_state}")
        if actor_type not in ALL_ACTORS:
            return self._error(task_id, None, target_state, ERROR_INVALID_ACTOR,
                              f"unknown actor type: {actor_type}")

        conn = self._get_conn()
        try:
            cur = conn.cursor()

            # ── Get current task state ──
            cur.execute(
                "SELECT id, status, state_version, project_id FROM development_tasks WHERE id = ?",
                (task_id,)
            )
            row = cur.fetchone()
            if row is None:
                return self._error(task_id, None, target_state, ERROR_TASK_NOT_FOUND,
                                  f"task {task_id} not found")

            current_status = (row["status"] or "").upper()
            current_version = row["state_version"] or 1
            project_id = row["project_id"]

            # Normalize to V2 state names if V1 status is stored
            current_state = self._normalize_state(current_status)

            # ── Idempotency check ──
            if idempotency_key:
                # Use raw expected_version in fingerprint (not computed default)
                # so repeated calls with same params match regardless of state changes
                idem_result = self._check_idempotency(
                    conn, task_id, idempotency_key,
                    target_state, actor_type, reason, expected_version, metadata
                )
                if idem_result is not None:
                    conn.close()
                    return idem_result

            # ── Check terminal ──
            if current_state in TERMINAL_STATES:
                conn.close()
                return self._error(task_id, current_state, target_state, ERROR_TERMINAL_STATE,
                                  f"task is in terminal state: {current_state}")

            # ── Check transition validity ──
            valid_targets = TRANSITION_GRAPH.get(current_state, frozenset())
            if target_state not in valid_targets:
                conn.close()
                return self._error(task_id, current_state, target_state,
                                  ERROR_INVALID_STATE_TRANSITION,
                                  f"cannot transition {current_state} → {target_state}")

            # ── Check actor authorization ──
            writable = ACTOR_WRITEABLE_STATES.get(actor_type, frozenset())
            if target_state not in writable:
                conn.close()
                return self._error(task_id, current_state, target_state,
                                  ERROR_ACTOR_NOT_AUTHORIZED,
                                  f"actor '{actor_type}' cannot write '{target_state}'")

            # ── Optimistic lock ──
            effective_expected = expected_version if expected_version is not None else current_version
            if effective_expected != current_version:
                conn.close()
                return self._error(task_id, current_state, target_state,
                                  ERROR_STATE_VERSION_CONFLICT,
                                  f"expected version {effective_expected}, actual {current_version}")

            # ── Execute transition (single transaction) ──
            conn.execute("BEGIN IMMEDIATE")
            try:
                new_version = current_version + 1
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # Update task state with optimistic WHERE clause
                cur.execute("""
                    UPDATE development_tasks
                    SET status = ?, state_version = ?, last_state_change = ?
                    WHERE id = ? AND state_version = ?
                """, (target_state.lower(), new_version, now, task_id, current_version))

                if cur.rowcount != 1:
                    conn.rollback()
                    conn.close()
                    return self._error(task_id, current_state, target_state,
                                      ERROR_STATE_VERSION_CONFLICT,
                                      "concurrent update detected")

                # ── Write event (inside same transaction) ──
                event_id = f"event-{uuid.uuid4().hex[:12]}"
                meta_json = json.dumps(metadata or {}, ensure_ascii=False)
                detail = {
                    "reason": reason,
                    "transition": f"{current_state} → {target_state}",
                }
                if idempotency_key:
                    detail["_fingerprint"] = {
                        "task_id": task_id,
                        "target_state": target_state,
                        "actor_type": actor_type,
                        "actor_id": actor_type,  # operator_id = actor_type for B1
                        "reason": reason,
                        # Use raw expected_version (None if not specified)
                        # so repeated calls with same params match
                        "expected_version": expected_version,
                        "metadata_hash": hashlib.sha256(
                            meta_json.encode()
                        ).hexdigest(),
                    }
                if metadata:
                    detail["metadata"] = metadata
                detail_json = json.dumps(detail, ensure_ascii=False)

                cur.execute("""
                    INSERT INTO task_events
                    (event_id, task_id, project_id, event_type,
                     from_state, to_state, reason, detail_json, operator_type, operator_id,
                     idempotency_key, state_version_before, state_version_after)
                    VALUES (?, ?, ?, 'state_change', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id, task_id, project_id,
                    current_state, target_state,
                    reason, detail_json,
                    actor_type, actor_type,  # operator_id = actor_type for B1
                    idempotency_key,
                    current_version, new_version,
                ))

                conn.commit()

                return {
                    "success": True,
                    "task_id": task_id,
                    "from_state": current_state,
                    "to_state": target_state,
                    "new_version": new_version,
                    "event_id": event_id,
                    "error": None,
                    "error_code": None,
                    "idempotent": False,
                }

            except Exception as e:
                conn.rollback()
                conn.close()
                return self._error(task_id, current_state, target_state,
                                  "TRANSACTION_FAILED", str(e))

        except Exception as e:
            conn.close()
            return self._error(task_id, "UNKNOWN", target_state, "ERROR", str(e))

    # ── Transition history ──

    def get_transition_history(self, task_id: int) -> Dict[str, Any]:
        """Get all state change events for a task, ordered by creation time.

        Returns:
            {"success": bool, "events": list, "error": str}
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT event_id, from_state, to_state, reason, operator_type,
                       operator_id, idempotency_key, state_version_before, state_version_after,
                       created_at, detail_json
                FROM task_events
                WHERE task_id = ?
                  AND event_type = 'state_change'
                ORDER BY created_at ASC
            """, (task_id,))
            events = [dict(row) for row in cur.fetchall()]
            return {"success": True, "events": events, "error": None}
        except Exception as e:
            return {"success": False, "events": [], "error": str(e)}
        finally:
            conn.close()

    # ── Helpers ──

    def _normalize_state(self, status: str) -> str:
        """Normalize V1 status strings to V2 STATE_* enums."""
        mapping = {
            "DRAFT": STATE_DRAFT,
            "PLANNED": STATE_PLANNED,
            "APPROVED": STATE_APPROVED,
            "QUEUED": STATE_QUEUED,
            "CLAIMED": STATE_CLAIMED,
            "RUNNING": STATE_RUNNING,
            "RESULT_SUBMITTED": STATE_RESULT_SUBMITTED,
            "REVIEWING": STATE_REVIEWING,
            "VERIFIED": STATE_VERIFIED,
            "REWORK": STATE_REWORK,
            "BLOCKED": STATE_BLOCKED,
            "NEED_USER": STATE_NEED_USER,
            "FAILED": STATE_FAILED,
            "CANCELLED": STATE_CANCELLED,
            # V1 compat
            "PENDING": STATE_DRAFT,
            "EXECUTING": STATE_RUNNING,
            "COMPLETED": STATE_VERIFIED,
            "WAITING_TEST": STATE_RUNNING,
            "TEST_FAILED": STATE_FAILED,
            "PAUSED": STATE_CANCELLED,
        }
        return mapping.get(status, STATE_DRAFT)

    def _check_idempotency(
        self, conn: sqlite3.Connection, task_id: int,
        idempotency_key: str, target_state: str,
        actor_type: str, reason: str,
        expected_version: int, metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Check if an idempotency key was already used.

        Compares full fingerprint: task_id, target_state, actor_type,
        actor_id, reason, expected_version, metadata hash.

        If same key exists with matching fingerprint → return cached result.
        If same key exists with different fingerprint → return IDEMPOTENCY_CONFLICT.
        """
        cur = conn.cursor()
        cur.execute("""
            SELECT event_id, from_state, to_state, state_version_after,
                   operator_type, operator_id, reason, detail_json,
                   state_version_before
            FROM task_events
            WHERE task_id = ? AND idempotency_key = ?
            LIMIT 1
        """, (task_id, idempotency_key))
        existing = cur.fetchone()
        if existing is None:
            return None  # not seen before

        existing_dict = dict(existing)

        # Build the new request fingerprint
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        new_fp = {
            "task_id": task_id,
            "target_state": target_state,
            "actor_type": actor_type,
            "actor_id": actor_type,  # operator_id = actor_type for B1
            "reason": reason,
            "expected_version": expected_version,
            "metadata_hash": hashlib.sha256(meta_json.encode()).hexdigest(),
        }

        # Parse stored fingerprint from detail_json
        stored_fp = None
        try:
            detail = json.loads(existing_dict.get("detail_json") or "{}")
            stored_fp = detail.get("_fingerprint")
        except (json.JSONDecodeError, TypeError):
            pass

        if stored_fp is not None:
            # Full fingerprint comparison
            matches = (
                stored_fp.get("task_id") == new_fp["task_id"] and
                stored_fp.get("target_state") == new_fp["target_state"] and
                stored_fp.get("actor_type") == new_fp["actor_type"] and
                stored_fp.get("actor_id") == new_fp["actor_id"] and
                stored_fp.get("reason") == new_fp["reason"] and
                stored_fp.get("expected_version") == new_fp["expected_version"] and
                stored_fp.get("metadata_hash") == new_fp["metadata_hash"]
            )
        else:
            # Backward compat: fingerprint not in detail_json, use simple match
            matches = (
                existing_dict.get("to_state") == target_state and
                existing_dict.get("operator_type") == actor_type
            )

        if matches:
            # Same request → return cached result
            return {
                "success": True,
                "task_id": task_id,
                "from_state": existing_dict["from_state"],
                "to_state": existing_dict["to_state"],
                "new_version": existing_dict["state_version_after"],
                "event_id": existing_dict["event_id"],
                "error": None,
                "error_code": None,
                "idempotent": True,
            }
        else:
            # Same key, different fingerprint → conflict
            return self._error(task_id, existing_dict["from_state"], target_state,
                              ERROR_IDEMPOTENCY_CONFLICT,
                              f"idempotency_key '{idempotency_key}' already used with different params")

    @staticmethod
    def _error(task_id: int, from_state: Optional[str], target_state: str,
               error_code: str, message: str) -> Dict[str, Any]:
        return {
            "success": False,
            "task_id": task_id,
            "from_state": from_state,
            "to_state": target_state,
            "new_version": None,
            "event_id": None,
            "error": message,
            "error_code": error_code,
            "idempotent": False,
        }
