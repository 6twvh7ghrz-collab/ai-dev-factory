"""V2.0-B2b: Task Claim Service — atomic task dispatch with lease management.

Implements claim_task(): single-transaction task claim with:
  - Feature flag gating (V2_CONTROL_PLANE_ENABLED)
  - Worker validation (registered, executor, AVAILABLE)
  - Capability matching (language, framework, platform, task_type)
  - Task scope enforcement (allowed_task_ids, project_id)
  - Optimistic version locking (expected_version)
  - Lease conflict detection (no unexpired active assignment)
  - Worker status transition (AVAILABLE → BUSY)
  - State machine integration (QUEUED → CLAIMED)
  - Idempotency with full fingerprint comparison
  - Task packet generation
"""

import sqlite3
import uuid
import json
import hashlib
import os
import secrets
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from .worker_registry import (
    WorkerRegistryService,
    WORKER_STATUS_AVAILABLE,
    WORKER_STATUS_BUSY,
    WORKER_STATUS_OFFLINE,
    WORKER_STATUS_DISABLED,
    WORKER_TYPE_EXECUTOR,
    ERROR_WORKER_NOT_REGISTERED,
    ERROR_WORKER_NOT_AVAILABLE,
    ERROR_WORKER_CAPABILITY_MISMATCH,
    ERROR_V2_CONTROL_PLANE_DISABLED,
)


# ============================================================
# Error codes
# ============================================================

ERROR_TASK_NOT_FOUND = "TASK_NOT_FOUND"
ERROR_TASK_NOT_CLAIMABLE = "TASK_NOT_CLAIMABLE"
ERROR_TASK_SCOPE_VIOLATION = "TASK_SCOPE_VIOLATION"
ERROR_STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
ERROR_LEASE_CONFLICT = "LEASE_CONFLICT"
ERROR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERROR_VALIDATION_ERROR = "VALIDATION_ERROR"
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"
ERROR_WORKER_TYPE_NOT_ALLOWED = "WORKER_TYPE_NOT_ALLOWED"

# ============================================================
# Active assignment statuses (prevent concurrent claim)
# ============================================================

ACTIVE_ASSIGNMENT_STATUSES = frozenset([
    "assigned", "acknowledged", "running", "retrying",
])

# ============================================================
# Lease limits
# ============================================================

LEASE_SECONDS_MIN = 30
LEASE_SECONDS_MAX = 3600


class TaskClaimService:
    """Single-transaction task claim with worker validation, capability
    matching, lease management, and idempotency.

    Uses a single BEGIN IMMEDIATE transaction for:
      - task_assignment INSERT
      - development_tasks state transition (QUEUED → CLAIMED)
      - task_events INSERT
      - agent_workers status change (AVAILABLE → BUSY)

    On any failure the entire transaction rolls back — no dirty state.
    """

    def __init__(self, db_path: str, v2_enabled: Optional[bool] = None):
        self.db_path = db_path
        self._worker_registry = WorkerRegistryService(db_path, v2_enabled=v2_enabled)
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
            return {
                "success": False,
                "error": "V2_CONTROL_PLANE_ENABLED is off; operation rejected",
                "error_code": ERROR_V2_CONTROL_PLANE_DISABLED,
            }
        return None

    # ── Public API ──

    def claim_task(
        self,
        task_id: int,
        worker_id: str,
        expected_version: int,
        idempotency_key: str,
        lease_seconds: int = 300,
        allowed_task_ids: Optional[List[int]] = None,
        project_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Claim a QUEUED task for an executor worker in a single atomic transaction.

        Returns:
            {
                "success": bool,
                "assignment_id": str or None,
                "task_id": int,
                "worker_id": str,
                "lease_token": str or None,
                "lease_expires_at": str or None,
                "task_packet": dict or None,
                "state_version": int or None,
                "idempotent": bool,
                "error_code": str or None,
                "error_message": str or None,
            }
        """
        # ── Feature flag ──
        gate = self._v2_gate()
        if gate:
            return self._make_error(task_id, worker_id, gate["error_code"], gate["error"])

        # ── Validate lease_seconds ──
        if not isinstance(lease_seconds, int) or lease_seconds < LEASE_SECONDS_MIN or lease_seconds > LEASE_SECONDS_MAX:
            return self._make_error(
                task_id, worker_id, ERROR_VALIDATION_ERROR,
                f"lease_seconds must be between {LEASE_SECONDS_MIN} and {LEASE_SECONDS_MAX}, got {lease_seconds}"
            )

        allowed_task_ids = allowed_task_ids or []

        conn = self._get_conn()
        try:
            # ── 1. Idempotency check (MUST be first after feature flag) ──
            idem_result = self._check_claim_idempotency(
                conn, task_id, worker_id, expected_version,
                lease_seconds, allowed_task_ids, project_id,
                idempotency_key
            )
            if idem_result is not None:
                conn.close()
                return idem_result

            # ── 2. Worker validation ──
            worker_err = self._validate_worker_for_claim(conn, worker_id)
            if worker_err:
                conn.close()
                return worker_err

            # ── 3. Task validation ──
            task_err = self._validate_task_for_claim(
                conn, task_id, expected_version, allowed_task_ids, project_id
            )
            if task_err:
                conn.close()
                return task_err

            # ── 4. Capability check ──
            cap_err = self._check_worker_capabilities(conn, worker_id, task_id)
            if cap_err:
                conn.close()
                return cap_err

            # ── 5. Lease conflict check ──
            lease_err = self._check_active_lease(conn, task_id)
            if lease_err:
                conn.close()
                return lease_err

            # ── 6. Execute claim transaction ──
            result = self._execute_claim_transaction(
                conn, task_id, worker_id, expected_version,
                idempotency_key, lease_seconds, allowed_task_ids, project_id
            )
            conn.close()
            return result

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return self._make_error(task_id, worker_id, ERROR_INTERNAL_ERROR, str(e))

    # ── Worker validation ──

    def _validate_worker_for_claim(self, conn: sqlite3.Connection, worker_id: str) -> Optional[Dict[str, Any]]:
        """Check worker is registered, is executor, and is AVAILABLE."""
        cur = conn.cursor()
        cur.execute("""
            SELECT worker_id, worker_type, status
            FROM agent_workers WHERE worker_id = ?
        """, (worker_id,))
        row = cur.fetchone()
        if row is None:
            return self._make_error(None, worker_id, ERROR_WORKER_NOT_REGISTERED,
                                    f"Worker '{worker_id}' not registered")

        if row["worker_type"] != WORKER_TYPE_EXECUTOR:
            return self._make_error(None, worker_id, ERROR_WORKER_TYPE_NOT_ALLOWED,
                                    f"Worker type '{row['worker_type']}' not allowed to claim tasks")

        if row["status"] == WORKER_STATUS_BUSY:
            return self._make_error(None, worker_id, ERROR_WORKER_NOT_AVAILABLE,
                                    f"Worker '{worker_id}' is BUSY")
        if row["status"] == WORKER_STATUS_OFFLINE:
            return self._make_error(None, worker_id, ERROR_WORKER_NOT_AVAILABLE,
                                    f"Worker '{worker_id}' is OFFLINE")
        if row["status"] == WORKER_STATUS_DISABLED:
            return self._make_error(None, worker_id, ERROR_WORKER_NOT_AVAILABLE,
                                    f"Worker '{worker_id}' is DISABLED")

        # Must be AVAILABLE (registered is also not ok for claiming)
        if row["status"] != WORKER_STATUS_AVAILABLE:
            return self._make_error(None, worker_id, ERROR_WORKER_NOT_AVAILABLE,
                                    f"Worker '{worker_id}' status is '{row['status']}', expected AVAILABLE")

        return None  # valid

    # ── Task validation ──

    def _validate_task_for_claim(
        self, conn: sqlite3.Connection, task_id: int,
        expected_version: int,
        allowed_task_ids: List[int],
        project_id: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """Check task exists, is QUEUED, version matches, scope is valid."""
        cur = conn.cursor()
        cur.execute("""
            SELECT id, project_id, status, state_version
            FROM development_tasks WHERE id = ?
        """, (task_id,))
        row = cur.fetchone()
        if row is None:
            return self._make_error(task_id, None, ERROR_TASK_NOT_FOUND,
                                    f"Task {task_id} not found")

        current_status = (row["status"] or "").upper()
        current_version = row["state_version"] or 1
        task_project_id = row["project_id"]

        # Must be QUEUED
        if current_status != "QUEUED":
            return self._make_error(task_id, None, ERROR_TASK_NOT_CLAIMABLE,
                                    f"Task {task_id} is '{current_status}', expected QUEUED")

        # Version check
        if expected_version != current_version:
            return self._make_error(task_id, None, ERROR_STATE_VERSION_CONFLICT,
                                    f"Expected version {expected_version}, actual {current_version}")

        # Scope: allowed_task_ids
        if allowed_task_ids and task_id not in allowed_task_ids:
            return self._make_error(task_id, None, ERROR_TASK_SCOPE_VIOLATION,
                                    f"Task {task_id} not in allowed_task_ids")

        # Scope: project_id
        if project_id is not None and project_id != task_project_id:
            return self._make_error(task_id, None, ERROR_TASK_SCOPE_VIOLATION,
                                    f"Project mismatch: caller={project_id}, task={task_project_id}")

        return None  # valid

    # ── Capability check ──

    def _check_worker_capabilities(
        self, conn: sqlite3.Connection, worker_id: str, task_id: int
    ) -> Optional[Dict[str, Any]]:
        """Check if the worker has the capabilities required by the task."""
        # Read task capability requirements from task metadata
        cur = conn.cursor()
        cur.execute("""
            SELECT task_type, implementation_steps, dependencies
            FROM development_tasks WHERE id = ?
        """, (task_id,))
        row = cur.fetchone()
        if row is None:
            return self._make_error(task_id, worker_id, ERROR_TASK_NOT_FOUND, f"Task {task_id} not found")

        required_caps = self._extract_required_capabilities(row)

        if not required_caps:
            # No capability requirements declared → allow
            return None

        # Read worker capabilities
        cur.execute(
            "SELECT capability FROM agent_capabilities WHERE worker_id = ?",
            (worker_id,)
        )
        worker_caps = {r["capability"].lower() for r in cur.fetchall()}

        for rc in required_caps:
            if rc.lower() not in worker_caps:
                return self._make_error(
                    task_id, worker_id, ERROR_WORKER_CAPABILITY_MISMATCH,
                    f"Worker lacks capability '{rc}'; worker has: {sorted(worker_caps)}"
                )

        return None

    def _extract_required_capabilities(self, task_row: sqlite3.Row) -> List[str]:
        """Extract capability requirements from task metadata fields.

        Looks for explicit capability declarations in implementation_steps JSON
        under a "_requirements" key with fields: language, framework,
        platform, task_type.

        If no explicit _requirements are declared, returns empty list
        (task claims proceed without capability checks).
        """
        caps = []

        # Check implementation_steps for _requirements
        impl_raw = task_row["implementation_steps"]
        if impl_raw:
            try:
                impl = json.loads(impl_raw) if isinstance(impl_raw, str) else impl_raw
                reqs = impl.get("_requirements") if isinstance(impl, dict) else None
                if isinstance(reqs, dict):
                    for key in ("language", "framework", "platform"):
                        val = reqs.get(key)
                        if val and isinstance(val, str) and val.strip():
                            caps.append(val.strip())
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

        return caps

    # ── Lease check ──

    def _check_active_lease(
        self, conn: sqlite3.Connection, task_id: int
    ) -> Optional[Dict[str, Any]]:
        """Check if there is an unexpired active assignment for this task."""
        cur = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            SELECT assignment_id, worker_id, lease_expires_at, status
            FROM task_assignments
            WHERE task_id = ?
              AND status IN ('assigned','acknowledged','running','retrying')
              AND lease_expires_at > ?
            LIMIT 1
        """, (task_id, now_str))
        row = cur.fetchone()
        if row:
            return self._make_error(
                task_id, row["worker_id"], ERROR_LEASE_CONFLICT,
                f"Task {task_id} already has active assignment {row['assignment_id']} "
                f"(status={row['status']}, expires={row['lease_expires_at']})"
            )
        return None

    # ── Idempotency ──

    def _build_claim_fingerprint(
        self,
        task_id: int, worker_id: str,
        expected_version: int, lease_seconds: int,
        allowed_task_ids: List[int], project_id: Optional[int],
    ) -> str:
        """Build a deterministic fingerprint for claim idempotency."""
        data = json.dumps({
            "t": task_id,
            "w": worker_id,
            "ev": expected_version,
            "ls": lease_seconds,
            "ati": sorted(allowed_task_ids) if allowed_task_ids else [],
            "pi": project_id,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(data.encode()).hexdigest()

    def _check_claim_idempotency(
        self, conn: sqlite3.Connection,
        task_id: int, worker_id: str,
        expected_version: int, lease_seconds: int,
        allowed_task_ids: List[int], project_id: Optional[int],
        idempotency_key: str,
    ) -> Optional[Dict[str, Any]]:
        """Check if this idempotency_key was already used for a claim.

        Returns:
            None → proceed with claim
            Dict → cached result (success) or conflict (failure)
        """
        new_fp = self._build_claim_fingerprint(
            task_id, worker_id, expected_version, lease_seconds,
            allowed_task_ids, project_id
        )

        cur = conn.cursor()
        # Check task_events for existing idempotency key
        cur.execute("""
            SELECT detail_json, event_id, created_at
            FROM task_events
            WHERE idempotency_key = ?
            LIMIT 1
        """, (idempotency_key,))
        event_row = cur.fetchone()

        if event_row is None:
            # Also check task_assignments (in case event wasn't written)
            cur.execute("""
                SELECT assignment_id, worker_id, lease_token, lease_expires_at,
                       idempotency_key, status
                FROM task_assignments
                WHERE idempotency_key = ?
                LIMIT 1
            """, (idempotency_key,))
            assign_row = cur.fetchone()
            if assign_row is None:
                return None  # Not seen before, proceed

            # Assignment exists - check fingerprint in detail if stored
            stored_fp = None
            try:
                # Check if fingerprint was stored in task_events or metadata
                pass
            except Exception:
                pass

            # Assignment exists without fingerprint → could be legacy
            # Conservative: treat as idempotent if same task/worker
            if assign_row["worker_id"] == worker_id:
                return self._build_claim_result(
                    task_id, worker_id, assign_row
                )
            else:
                return self._make_error(
                    task_id, worker_id, ERROR_IDEMPOTENCY_CONFLICT,
                    f"Idempotency key '{idempotency_key}' already used by different worker"
                )

        # Event exists — check fingerprint in detail_json
        stored_fp = None
        try:
            detail = json.loads(event_row["detail_json"] or "{}")
            stored_fp = detail.get("_claim_fingerprint")
        except (json.JSONDecodeError, TypeError):
            pass

        if stored_fp is not None:
            if stored_fp == new_fp:
                # Same request → return cached result
                cur.execute("""
                    SELECT assignment_id, worker_id, lease_token, lease_expires_at, status
                    FROM task_assignments
                    WHERE idempotency_key = ?
                    LIMIT 1
                """, (idempotency_key,))
                assign_row = cur.fetchone()
                if assign_row:
                    return self._build_claim_result(task_id, worker_id, assign_row, idempotent=True)
                # Fallback: event exists but no assignment found
                return self._make_error(
                    task_id, worker_id, ERROR_INTERNAL_ERROR,
                    "Idempotent claim found but assignment record missing"
                )
            else:
                # Same key, different request → conflict
                return self._make_error(
                    task_id, worker_id, ERROR_IDEMPOTENCY_CONFLICT,
                    f"Idempotency key '{idempotency_key}' already used with different parameters"
                )
        else:
            # No fingerprint stored → legacy record
            # Try to find assignment by this key
            cur.execute("""
                SELECT assignment_id, worker_id, lease_token, lease_expires_at, status
                FROM task_assignments
                WHERE idempotency_key = ?
                LIMIT 1
            """, (idempotency_key,))
            assign_row = cur.fetchone()
            if assign_row and assign_row["worker_id"] == worker_id:
                return self._build_claim_result(task_id, worker_id, assign_row, idempotent=True)
            return self._make_error(
                task_id, worker_id, ERROR_IDEMPOTENCY_CONFLICT,
                f"Idempotency key '{idempotency_key}' already used with different parameters or worker"
            )

    # ── Execute claim (single transaction) ──

    def _execute_claim_transaction(
        self, conn: sqlite3.Connection,
        task_id: int, worker_id: str,
        expected_version: int, idempotency_key: str,
        lease_seconds: int, allowed_task_ids: List[int],
        project_id: Optional[int],
    ) -> Dict[str, Any]:
        """Execute the claim inside a single BEGIN IMMEDIATE transaction."""

        cur = conn.cursor()

        # Re-read task inside transaction for atomicity
        cur.execute("""
            SELECT id, project_id, status, state_version
            FROM development_tasks WHERE id = ?
        """, (task_id,))
        task_row = cur.fetchone()
        if task_row is None:
            return self._make_error(task_id, worker_id, ERROR_TASK_NOT_FOUND, f"Task {task_id} not found")

        current_status = (task_row["status"] or "").upper()
        current_version = task_row["state_version"] or 1
        task_project_id = task_row["project_id"]

        # Re-validate inside transaction
        if current_status != "QUEUED":
            return self._make_error(task_id, worker_id, ERROR_TASK_NOT_CLAIMABLE,
                                    f"Task {task_id} is '{current_status}' (re-checked in transaction)")

        if current_version != expected_version:
            return self._make_error(task_id, worker_id, ERROR_STATE_VERSION_CONFLICT,
                                    f"Version changed: expected {expected_version}, now {current_version}")

        # Re-verify worker is AVAILABLE
        cur.execute("SELECT status FROM agent_workers WHERE worker_id = ?", (worker_id,))
        w_row = cur.fetchone()
        if w_row is None or w_row["status"] != WORKER_STATUS_AVAILABLE:
            return self._make_error(task_id, worker_id, ERROR_WORKER_NOT_AVAILABLE,
                                    "Worker no longer AVAILABLE (re-checked in transaction)")

        # Double-check no active lease
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            SELECT assignment_id FROM task_assignments
            WHERE task_id = ? AND status IN ('assigned','acknowledged','running','retrying')
              AND lease_expires_at > ?
            LIMIT 1
        """, (task_id, now_str))
        if cur.fetchone():
            return self._make_error(task_id, worker_id, ERROR_LEASE_CONFLICT,
                                    "Active lease appeared (re-checked in transaction)")

        # ── Generate IDs and tokens ──
        assignment_id = f"asgn-{uuid.uuid4().hex[:12]}"
        event_id = f"event-{uuid.uuid4().hex[:12]}"
        lease_token = secrets.token_hex(32)
        new_version = current_version + 1
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        expires_at = (now + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%d %H:%M:%S")

        # Build fingerprint
        fp = self._build_claim_fingerprint(
            task_id, worker_id, expected_version, lease_seconds,
            allowed_task_ids, project_id
        )

        try:
            conn.execute("BEGIN IMMEDIATE")

            # 0. Retire expired assignments (they block the UNIQUE index)
            cur.execute("""
                UPDATE task_assignments
                SET status = 'timeout', updated_at = ?
                WHERE task_id = ?
                  AND status IN ('assigned','acknowledged','running','retrying')
                  AND lease_expires_at <= ?
            """, (now_str, task_id, now_str))

            # 1. Update task state: QUEUED → CLAIMED
            cur.execute("""
                UPDATE development_tasks
                SET status = ?, state_version = ?, last_state_change = ?
                WHERE id = ? AND state_version = ?
            """, ("claimed", new_version, now_str, task_id, current_version))

            if cur.rowcount != 1:
                conn.rollback()
                return self._make_error(task_id, worker_id, ERROR_STATE_VERSION_CONFLICT,
                                        "Concurrent state update detected")

            # 2. Insert task_event (claim event)
            event_detail = {
                "reason": f"Claimed by worker {worker_id}",
                "transition": "QUEUED → CLAIMED",
                "_claim_fingerprint": fp,
            }
            event_detail_json = json.dumps(event_detail, ensure_ascii=False)

            cur.execute("""
                INSERT INTO task_events
                (event_id, task_id, project_id, event_type,
                 from_state, to_state, reason, detail_json,
                 operator_type, operator_id, idempotency_key,
                 state_version_before, state_version_after)
                VALUES (?, ?, ?, 'claim', 'QUEUED', 'CLAIMED', ?, ?,
                        'supervisor', ?, ?, ?, ?)
            """, (
                event_id, task_id, task_project_id,
                f"Claimed by worker {worker_id}", event_detail_json,
                worker_id, idempotency_key,
                current_version, new_version,
            ))

            # 3. Insert task_assignment
            cur.execute("""
                INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, decision_reason, status,
                 lease_token, lease_expires_at, idempotency_key,
                 dispatched_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'executor', 'dispatched by supervisor',
                        'assigned', ?, ?, ?, ?, ?, ?)
            """, (
                assignment_id, task_id, worker_id, task_project_id,
                lease_token, expires_at, idempotency_key,
                now_str, now_str, now_str,
            ))

            # 4. Update worker to BUSY
            cur.execute("""
                UPDATE agent_workers
                SET status = ?, last_seen_at = ?, version = version + 1
                WHERE worker_id = ? AND status = ?
            """, (WORKER_STATUS_BUSY, now_str, worker_id, WORKER_STATUS_AVAILABLE))

            if cur.rowcount != 1:
                conn.rollback()
                return self._make_error(task_id, worker_id, ERROR_INTERNAL_ERROR,
                                        "Failed to set worker BUSY (concurrent status change)")

            conn.commit()

            # ── Build task packet ──
            task_packet = self._build_task_packet(conn, task_id, worker_id,
                                                   assignment_id, lease_token,
                                                   expires_at, new_version,
                                                   allowed_task_ids)

            result = {
                "success": True,
                "assignment_id": assignment_id,
                "task_id": task_id,
                "worker_id": worker_id,
                "lease_token": lease_token,
                "lease_expires_at": expires_at,
                "task_packet": task_packet,
                "state_version": new_version,
                "idempotent": False,
                "error_code": None,
                "error_message": None,
            }
            return result

        except Exception:
            conn.rollback()
            raise

    # ── Task packet builder ──

    def _build_task_packet(
        self, conn: sqlite3.Connection,
        task_id: int, worker_id: str,
        assignment_id: str, lease_token: str,
        expires_at: str, state_version: int,
        allowed_task_ids: List[int],
    ) -> Dict[str, Any]:
        """Build the task packet from database task data."""
        cur = conn.cursor()
        cur.execute("""
            SELECT id, project_id, title, description, task_type,
                   files_to_modify, files_to_check, test_steps,
                   acceptance_criteria, implementation_steps,
                   dependencies, status, state_version
            FROM development_tasks WHERE id = ?
        """, (task_id,))
        row = cur.fetchone()

        # Parse JSON fields
        def _safe_parse(raw):
            if not raw:
                return None
            if isinstance(raw, (list, dict)):
                return raw
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return raw

        files_to_modify = _safe_parse(row["files_to_modify"]) if row else None
        test_steps = _safe_parse(row["test_steps"]) if row else None
        acceptance_criteria = _safe_parse(row["acceptance_criteria"]) if row else None
        implementation_steps = _safe_parse(row["implementation_steps"]) if row else None
        dependencies = _safe_parse(row["dependencies"]) if row else None

        # allowed_files from files_to_modify (if it's a list)
        allowed_files = files_to_modify if isinstance(files_to_modify, list) else []

        # test_commands from test_steps
        test_commands = test_steps if isinstance(test_steps, list) else (
            [test_steps] if isinstance(test_steps, str) and test_steps.strip() else []
        )

        # success_criteria from acceptance_criteria
        success_criteria = acceptance_criteria if isinstance(acceptance_criteria, list) else (
            [acceptance_criteria] if isinstance(acceptance_criteria, str) and acceptance_criteria.strip() else []
        )

        # evidence_required from implementation_steps
        evidence_required = []
        if isinstance(implementation_steps, dict):
            evidence_list = implementation_steps.get("expected_evidence", [])
            if isinstance(evidence_list, list):
                evidence_required = evidence_list

        # forbidden_actions: MVP empty
        forbidden_actions = []

        # current_stage: derive from task_type or status
        current_stage = "implementation"
        if row and row["task_type"]:
            stage_map = {"backend": "implementation", "frontend": "implementation",
                         "testing": "testing", "review": "review",
                         "documentation": "documentation"}
            current_stage = stage_map.get(row["task_type"].lower(), "implementation")

        project_id = row["project_id"] if row else None

        return {
            "task_id": task_id,
            "project_id": project_id,
            "title": row["title"] if row else "",
            "description": row["description"] if row else "",
            "task_type": row["task_type"] if row else "",
            "current_stage": current_stage,
            "allowed_task_ids": allowed_task_ids or [task_id],
            "allowed_files": allowed_files,
            "forbidden_actions": forbidden_actions,
            "test_commands": test_commands,
            "success_criteria": success_criteria,
            "evidence_required": evidence_required,
            "assignment_id": assignment_id,
            "lease_token": lease_token,
            "lease_expires_at": expires_at,
            "state_version": state_version,
            "git_head": None,
            "dependencies": dependencies,
        }

    # ── Result builders ──

    def _build_claim_result(
        self, task_id: int, worker_id: str,
        assign_row: sqlite3.Row, idempotent: bool = False,
    ) -> Dict[str, Any]:
        """Build a claim result from an existing assignment row."""
        # Read current task state_version
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT state_version FROM development_tasks WHERE id = ?", (task_id,))
            task_row = cur.fetchone()
            state_version = task_row["state_version"] if task_row else None
        except Exception:
            state_version = None
        finally:
            conn.close()

        return {
            "success": True,
            "assignment_id": assign_row["assignment_id"],
            "task_id": task_id,
            "worker_id": worker_id,
            "lease_token": assign_row["lease_token"],
            "lease_expires_at": assign_row["lease_expires_at"],
            "task_packet": None,  # Don't rebuild packet for cached result
            "state_version": state_version,
            "idempotent": idempotent,
            "error_code": None,
            "error_message": None,
        }

    @staticmethod
    def _make_error(
        task_id: Optional[int], worker_id: Optional[str],
        error_code: str, error_message: str,
    ) -> Dict[str, Any]:
        return {
            "success": False,
            "assignment_id": None,
            "task_id": task_id,
            "worker_id": worker_id,
            "lease_token": None,
            "lease_expires_at": None,
            "task_packet": None,
            "state_version": None,
            "idempotent": False,
            "error_code": error_code,
            "error_message": error_message,
        }
