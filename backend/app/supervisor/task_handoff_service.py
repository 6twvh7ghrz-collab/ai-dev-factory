"""V2.0-B4: task handoff packets and transfer workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import unquote


ERROR_V2_CONTROL_PLANE_DISABLED = "V2_CONTROL_PLANE_DISABLED"
ERROR_VALIDATION_ERROR = "VALIDATION_ERROR"
ERROR_HANDOFF_NOT_FOUND = "HANDOFF_NOT_FOUND"
ERROR_HANDOFF_NOT_ALLOWED = "HANDOFF_NOT_ALLOWED"
ERROR_HANDOFF_CONFLICT = "HANDOFF_CONFLICT"
ERROR_HANDOFF_EXPIRED = "HANDOFF_EXPIRED"
ERROR_WORKER_NOT_REGISTERED = "WORKER_NOT_REGISTERED"
ERROR_WORKER_NOT_AVAILABLE = "WORKER_NOT_AVAILABLE"
ERROR_WORKER_TYPE_NOT_ALLOWED = "WORKER_TYPE_NOT_ALLOWED"
ERROR_WORKER_CAPABILITY_MISMATCH = "WORKER_CAPABILITY_MISMATCH"
ERROR_ASSIGNMENT_NOT_FOUND = "ASSIGNMENT_NOT_FOUND"
ERROR_LEASE_CONFLICT = "LEASE_CONFLICT"
ERROR_STALE_LEASE = "STALE_LEASE"
ERROR_TASK_SCOPE_VIOLATION = "TASK_SCOPE_VIOLATION"
ERROR_STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
ERROR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"

ACTIVE_ASSIGNMENT_STATUSES = {"assigned", "acknowledged", "running", "retrying"}
HANDOFF_TASK_STATES = {"claimed", "running", "result_submitted", "rework", "CLAIMED", "RUNNING", "RESULT_SUBMITTED", "REWORK"}
ALLOWED_REASON_CODES = {
    "QUOTA_EXHAUSTED",
    "WORKER_OFFLINE",
    "TOOLCHAIN_UNAVAILABLE",
    "CAPABILITY_MISMATCH",
    "MANUAL_REASSIGN",
    "REWORK_REQUIRED",
    "worker_unresponsive",
    "worker_error",
    "worker_disconnect",
    "user_request",
    "budget_exceeded",
    "manual_reassign",
    "preemption",
}
SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "database_url", "password", "secret", "token", "lease_token"}
SENSITIVE_COMPACTS = {re.sub(r"[^a-z0-9]", "", key) for key in SENSITIVE_KEYS}


class TaskHandoffService:
    def __init__(self, db_path: str, v2_enabled: Optional[bool] = None):
        self.db_path = db_path
        self._v2_enabled = (
            v2_enabled if v2_enabled is not None
            else os.getenv("V2_CONTROL_PLANE_ENABLED", "false").lower() in ("true", "1")
        )

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        conn.row_factory = sqlite3.Row
        return conn

    def request_handoff(
        self,
        task_id: int,
        assignment_id: str,
        from_worker_id: str,
        lease_token: str,
        reason_code: str,
        reason: str,
        completed_steps: List[Any],
        remaining_steps: List[Any],
        recent_errors: List[Any],
        evidence_refs: List[str],
        forbidden_actions: List[str],
        idempotency_key: str,
        files_changed: Optional[List[str]] = None,
        tests_run: Optional[List[Any]] = None,
        context_snapshot: Optional[Dict[str, Any]] = None,
        git_head: str = "",
        current_stage: str = "",
        expires_seconds: int = 3600,
    ) -> Dict[str, Any]:
        gate = self._gate()
        if gate:
            return gate
        err = self._validate_request_inputs(task_id, assignment_id, from_worker_id, lease_token, reason_code, idempotency_key)
        if err:
            return err
        files_changed = files_changed or []
        tests_run = tests_run or []
        context_snapshot = context_snapshot or {}
        packet = {
            "completed_steps": completed_steps,
            "remaining_steps": remaining_steps,
            "recent_errors": recent_errors,
            "evidence_refs": evidence_refs,
            "forbidden_actions": forbidden_actions,
            "files_changed": files_changed,
            "tests_run": tests_run,
            "context_snapshot": context_snapshot,
            "git_head": git_head,
            "current_stage": current_stage,
        }
        packet_error = self._validate_packet(packet)
        if packet_error:
            return packet_error
        fingerprint = self._canonical_hash({
            "op": "request",
            "task_id": task_id,
            "assignment_id": assignment_id,
            "from_worker_id": from_worker_id,
            "lease_token_hash": hashlib.sha256(lease_token.encode()).hexdigest(),
            "reason_code": reason_code,
            "reason": reason,
            "packet_hash": self._canonical_hash(packet),
        })
        conn = self._get_conn()
        try:
            idem = self._check_idempotency(conn, idempotency_key, fingerprint)
            if idem:
                conn.close()
                return idem
            pre = self._validate_source(conn, task_id, assignment_id, from_worker_id, lease_token)
            if not pre["success"]:
                conn.close()
                return pre
            result = self._execute_request(conn, pre["task"], pre["assignment"], reason_code, reason, packet, idempotency_key, fingerprint, expires_seconds)
            conn.close()
            return result
        except Exception as exc:
            self._rollback_close(conn)
            return self._error(ERROR_INTERNAL_ERROR, str(exc))

    def accept_handoff(self, handoff_id: str, to_worker_id: str, expected_version: int, idempotency_key: str, lease_seconds: int = 300) -> Dict[str, Any]:
        gate = self._gate()
        if gate:
            return gate
        if not handoff_id or not to_worker_id or not idempotency_key:
            return self._error(ERROR_VALIDATION_ERROR, "handoff_id, to_worker_id and idempotency_key are required")
        fingerprint = self._canonical_hash({"op": "accept", "handoff_id": handoff_id, "to_worker_id": to_worker_id, "expected_version": expected_version, "lease_seconds": lease_seconds})
        conn = self._get_conn()
        try:
            idem = self._check_event_idempotency(conn, f"handoff-accept:{idempotency_key}", fingerprint)
            if idem:
                conn.close()
                return idem
            handoff = self._get_handoff(conn, handoff_id)
            if handoff is None:
                conn.close()
                return self._error(ERROR_HANDOFF_NOT_FOUND, "Handoff not found")
            if handoff["status"] != "pending":
                conn.close()
                return self._error(ERROR_HANDOFF_CONFLICT, "Handoff is no longer pending")
            if self._is_expired(handoff["expires_at"]):
                conn.close()
                return self._error(ERROR_HANDOFF_EXPIRED, "Handoff is expired")
            worker_err = self._validate_accept_worker(conn, to_worker_id, handoff)
            if worker_err:
                conn.close()
                return worker_err
            task = self._get_task(conn, int(handoff["task_id"]))
            if task is None or int(task["state_version"] or 1) != expected_version:
                conn.close()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task version changed")
            cap_err = self._check_capabilities(conn, to_worker_id, task)
            if cap_err:
                conn.close()
                return cap_err
            result = self._execute_accept(conn, handoff, to_worker_id, expected_version, lease_seconds, idempotency_key, fingerprint)
            conn.close()
            return result
        except Exception as exc:
            self._rollback_close(conn)
            return self._error(ERROR_INTERNAL_ERROR, str(exc))

    def reject_handoff(self, handoff_id: str, worker_id: str, reason: str, idempotency_key: str) -> Dict[str, Any]:
        return self._terminal_handoff(handoff_id, worker_id, reason, idempotency_key, "rejected", "handoff-reject", candidate_only=True)

    def cancel_handoff(self, handoff_id: str, actor_id: str, reason: str, idempotency_key: str) -> Dict[str, Any]:
        return self._terminal_handoff(handoff_id, actor_id, reason, idempotency_key, "cancelled", "handoff-cancel", candidate_only=False)

    def expire_handoffs(self, idempotency_key: str) -> Dict[str, Any]:
        gate = self._gate()
        if gate:
            return gate
        conn = self._get_conn()
        try:
            fingerprint = self._canonical_hash({"op": "expire", "now_date": self._now()[:10]})
            idem = self._check_event_idempotency(conn, f"handoff-expire:{idempotency_key}", fingerprint)
            if idem:
                conn.close()
                return idem
            now = self._now()
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM task_handoffs WHERE status='pending' AND expires_at <= ?", (now,)).fetchall()
            for row in rows:
                conn.execute("UPDATE task_handoffs SET status='expired', expired_at=?, updated_at=? WHERE handoff_id=? AND status='pending'", (now, now, row["handoff_id"]))
            if not rows:
                conn.commit()
                result = {"success": True, "status": "expired", "expired_count": 0, "idempotent": False, "error_code": None}
                conn.close()
                return result
            event_id = f"event-{uuid.uuid4().hex[:16]}"
            first = rows[0]
            detail = {"status": "expired", "expired_count": len(rows), "_fingerprint": fingerprint}
            conn.execute("""
                INSERT INTO task_events
                (event_id, task_id, project_id, event_type, from_state, to_state, reason, detail_json,
                 operator_type, operator_id, idempotency_key)
                VALUES (?, ?, ?, 'handoff', 'PENDING', 'EXPIRED', 'Expired pending handoffs', ?, 'system', 'system', ?)
            """, (event_id, first["task_id"], first["project_id"], self._json(detail), f"handoff-expire:{idempotency_key}"))
            conn.commit()
            result = {"success": True, "status": "expired", "expired_count": len(rows), "idempotent": False, "error_code": None}
            conn.close()
            return result
        except Exception as exc:
            self._rollback_close(conn)
            return self._error(ERROR_INTERNAL_ERROR, str(exc))

    def _execute_request(self, conn, task, assignment, reason_code, reason, packet, idempotency_key, fingerprint, expires_seconds):
        handoff_id = f"hndf-{uuid.uuid4().hex[:16]}"
        now = self._now()
        expires_at = (datetime.now() + timedelta(seconds=expires_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute("SELECT 1 FROM task_handoffs WHERE from_assignment_id=? AND status='pending'", (assignment["assignment_id"],)).fetchone()
            if existing:
                conn.rollback()
                return self._error(ERROR_HANDOFF_CONFLICT, "Assignment already has pending handoff")
            conn.execute("""
                INSERT INTO task_handoffs
                (handoff_id, task_id, project_id, from_assignment_id, from_worker_id, status,
                 reason_code, reason, current_task_state, current_stage, completed_steps_json,
                 remaining_steps_json, files_changed_json, tests_run_json, recent_errors_json,
                 evidence_refs_json, forbidden_actions_json, context_snapshot_json, git_head,
                 expires_at, idempotency_key, request_fingerprint, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                handoff_id, task["id"], task["project_id"], assignment["assignment_id"], assignment["worker_id"],
                reason_code, reason, task["status"], packet["current_stage"], self._json(packet["completed_steps"]),
                self._json(packet["remaining_steps"]), self._json(packet["files_changed"]), self._json(packet["tests_run"]),
                self._json(packet["recent_errors"]), self._json(packet["evidence_refs"]), self._json(packet["forbidden_actions"]),
                self._json(packet["context_snapshot"]), packet["git_head"], expires_at, idempotency_key, fingerprint, now, now,
            ))
            event_id = f"event-{uuid.uuid4().hex[:16]}"
            detail = {"handoff_id": handoff_id, "reason_code": reason_code, "_fingerprint": fingerprint}
            conn.execute("""
                INSERT INTO task_events
                (event_id, task_id, assignment_id, project_id, event_type, from_state, to_state, reason,
                 detail_json, operator_type, operator_id, idempotency_key, state_version_before, state_version_after)
                VALUES (?, ?, ?, ?, 'handoff', ?, ?, ?, ?, 'worker', ?, ?, ?, ?)
            """, (
                event_id, task["id"], assignment["assignment_id"], task["project_id"], task["status"].upper(),
                task["status"].upper(), reason, self._json(detail), assignment["worker_id"], f"handoff-request:{idempotency_key}",
                task["state_version"], task["state_version"],
            ))
            conn.commit()
            return self._success(handoff_id, task["id"], task["project_id"], "pending", assignment["worker_id"], None, expires_at, False)
        except Exception:
            conn.rollback()
            raise

    def _execute_accept(self, conn, handoff, to_worker_id, expected_version, lease_seconds, idempotency_key, fingerprint):
        now = self._now()
        lease_token = secrets.token_hex(32)
        lease_expires_at = (datetime.now() + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        new_assignment_id = f"asgn-{uuid.uuid4().hex[:16]}"
        conn.execute("BEGIN IMMEDIATE")
        try:
            h = conn.execute("SELECT * FROM task_handoffs WHERE handoff_id=?", (handoff["handoff_id"],)).fetchone()
            if h is None or h["status"] != "pending":
                conn.rollback()
                return self._error(ERROR_HANDOFF_CONFLICT, "Handoff is no longer pending")
            task = self._get_task(conn, int(h["task_id"]))
            if task is None or int(task["state_version"] or 1) != expected_version:
                conn.rollback()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task version changed")
            prior_state = str(task["status"] or "").lower()
            old_assignment = conn.execute("SELECT * FROM task_assignments WHERE assignment_id=?", (h["from_assignment_id"],)).fetchone()
            if old_assignment is None or old_assignment["status"] not in ACTIVE_ASSIGNMENT_STATUSES:
                if prior_state != "rework" or old_assignment is None or old_assignment["status"] != "completed":
                    conn.rollback()
                    return self._error(ERROR_ASSIGNMENT_NOT_FOUND, "Old assignment is not active")
            allowed_statuses = ("assigned", "acknowledged", "running", "retrying")
            if prior_state == "rework":
                allowed_statuses = allowed_statuses + ("completed",)
            placeholders = ",".join("?" for _ in allowed_statuses)
            cur = conn.execute(
                f"UPDATE task_assignments SET status='cancelled', updated_at=? WHERE assignment_id=? AND status IN ({placeholders})",
                (now, h["from_assignment_id"], *allowed_statuses),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return self._error(ERROR_HANDOFF_CONFLICT, "Old assignment changed")
            conn.execute("""
                INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id, agent_type_required, status,
                 lease_token, lease_expires_at, idempotency_key, dispatched_at, started_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'executor', 'running', ?, ?, ?, ?, ?, ?, ?)
            """, (new_assignment_id, h["task_id"], to_worker_id, h["project_id"], lease_token, lease_expires_at, f"handoff-accept:{idempotency_key}", now, now, now, now))
            cur = conn.execute("UPDATE agent_workers SET status='busy', current_load=1, last_seen_at=?, version=version+1 WHERE worker_id=? AND status='available'", (now, to_worker_id))
            if cur.rowcount != 1:
                conn.rollback()
                return self._error(ERROR_WORKER_NOT_AVAILABLE, "New worker is not available")
            remaining_old = conn.execute("SELECT COUNT(*) AS c FROM task_assignments WHERE worker_id=? AND status IN ('assigned','acknowledged','running','retrying')", (h["from_worker_id"],)).fetchone()["c"]
            if int(remaining_old) == 0:
                conn.execute("UPDATE agent_workers SET status='registered', current_load=0, last_seen_at=?, version=version+1 WHERE worker_id=?", (now, h["from_worker_id"]))
            task_version_after = expected_version
            task_to_state = task["status"].lower()
            if prior_state == "rework":
                task_to_state = "running"
                task_version_after = expected_version + 1
                cur = conn.execute(
                    "UPDATE development_tasks SET status='running', state_version=?, last_state_change=? WHERE id=? AND state_version=?",
                    (task_version_after, now, h["task_id"], expected_version),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return self._error(ERROR_STATE_VERSION_CONFLICT, "Task version changed")
            conn.execute("UPDATE task_handoffs SET status='accepted', to_worker_id=?, accepted_at=?, updated_at=? WHERE handoff_id=? AND status='pending'", (to_worker_id, now, now, h["handoff_id"]))
            event_id = f"event-{uuid.uuid4().hex[:16]}"
            detail = {"handoff_id": h["handoff_id"], "new_assignment_id": new_assignment_id, "_fingerprint": fingerprint}
            conn.execute("""
                INSERT INTO task_events
                (event_id, task_id, assignment_id, project_id, event_type, from_state, to_state, reason,
                 detail_json, operator_type, operator_id, idempotency_key, state_version_before, state_version_after)
                VALUES (?, ?, ?, ?, 'handoff', ?, ?, 'Handoff accepted', ?, 'worker', ?, ?, ?, ?)
            """, (event_id, h["task_id"], new_assignment_id, h["project_id"], prior_state.upper(), task_to_state.upper(), self._json(detail), to_worker_id, f"handoff-accept:{idempotency_key}", expected_version, task_version_after))
            conn.commit()
            return {**self._success(h["handoff_id"], h["task_id"], h["project_id"], "accepted", h["from_worker_id"], to_worker_id, h["expires_at"], False), "assignment_id": new_assignment_id, "lease_token": lease_token, "lease_expires_at": lease_expires_at}
        except Exception:
            conn.rollback()
            raise

    def _terminal_handoff(self, handoff_id, actor_id, reason, idempotency_key, status, event_prefix, candidate_only):
        gate = self._gate()
        if gate:
            return gate
        fingerprint = self._canonical_hash({"op": status, "handoff_id": handoff_id, "actor_id": actor_id, "reason": reason})
        conn = self._get_conn()
        try:
            idem = self._check_event_idempotency(conn, f"{event_prefix}:{idempotency_key}", fingerprint)
            if idem:
                conn.close()
                return idem
            handoff = self._get_handoff(conn, handoff_id)
            if handoff is None:
                conn.close()
                return self._error(ERROR_HANDOFF_NOT_FOUND, "Handoff not found")
            if handoff["status"] != "pending":
                conn.close()
                return self._error(ERROR_HANDOFF_CONFLICT, "Handoff is no longer pending")
            if candidate_only and (not handoff["to_worker_id"] or handoff["to_worker_id"] != actor_id):
                conn.close()
                return self._error(ERROR_HANDOFF_NOT_ALLOWED, "Worker is not the handoff candidate")
            if not candidate_only and actor_id not in (handoff["from_worker_id"], "supervisor", "system"):
                conn.close()
                return self._error(ERROR_HANDOFF_NOT_ALLOWED, "Actor cannot cancel this handoff")
            now = self._now()
            conn.execute("BEGIN IMMEDIATE")
            col = {"rejected": "rejected_at", "cancelled": "cancelled_at"}[status]
            conn.execute(f"UPDATE task_handoffs SET status=?, {col}=?, updated_at=? WHERE handoff_id=? AND status='pending'", (status, now, now, handoff_id))
            event_id = f"event-{uuid.uuid4().hex[:16]}"
            detail = {"handoff_id": handoff_id, "status": status, "_fingerprint": fingerprint}
            conn.execute("""
                INSERT INTO task_events
                (event_id, task_id, assignment_id, project_id, event_type, from_state, to_state, reason,
                 detail_json, operator_type, operator_id, idempotency_key)
                VALUES (?, ?, ?, ?, 'handoff', 'PENDING', ?, ?, ?, 'worker', ?, ?)
            """, (event_id, handoff["task_id"], handoff["from_assignment_id"], handoff["project_id"], status.upper(), reason, self._json(detail), actor_id, f"{event_prefix}:{idempotency_key}"))
            conn.commit()
            result = self._success(handoff_id, handoff["task_id"], handoff["project_id"], status, handoff["from_worker_id"], handoff["to_worker_id"], handoff["expires_at"], False)
            conn.close()
            return result
        except Exception as exc:
            self._rollback_close(conn)
            return self._error(ERROR_INTERNAL_ERROR, str(exc))

    def _validate_source(self, conn, task_id, assignment_id, worker_id, lease_token):
        task = self._get_task(conn, task_id)
        if task is None:
            return self._error(ERROR_TASK_SCOPE_VIOLATION, "Task not found")
        if task["status"] not in HANDOFF_TASK_STATES:
            return self._error(ERROR_HANDOFF_NOT_ALLOWED, "Task state does not allow handoff")
        assignment = conn.execute("SELECT * FROM task_assignments WHERE assignment_id=?", (assignment_id,)).fetchone()
        if assignment is None:
            return self._error(ERROR_ASSIGNMENT_NOT_FOUND, "Assignment not found")
        if int(assignment["task_id"]) != task_id:
            return self._error(ERROR_TASK_SCOPE_VIOLATION, "Assignment does not belong to task")
        if assignment["worker_id"] != worker_id:
            return self._error(ERROR_LEASE_CONFLICT, "Assignment belongs to another worker")
        if assignment["lease_token"] != lease_token:
            return self._error(ERROR_LEASE_CONFLICT, "Lease token does not match")
        if assignment["status"] not in ACTIVE_ASSIGNMENT_STATUSES:
            if str(task["status"]).lower() != "rework" or assignment["status"] != "completed":
                return self._error(ERROR_HANDOFF_NOT_ALLOWED, "Assignment is not active")
        if self._is_expired(assignment["lease_expires_at"]):
            return self._error(ERROR_STALE_LEASE, "Lease is expired")
        return {"success": True, "task": task, "assignment": assignment}

    def _validate_accept_worker(self, conn, worker_id, handoff):
        worker = conn.execute("SELECT worker_id, worker_type, status FROM agent_workers WHERE worker_id=?", (worker_id,)).fetchone()
        if worker is None:
            return self._error(ERROR_WORKER_NOT_REGISTERED, "Worker not registered")
        if worker["worker_type"] != "executor":
            return self._error(ERROR_WORKER_TYPE_NOT_ALLOWED, "Worker must be executor")
        if worker["status"] != "available":
            return self._error(ERROR_WORKER_NOT_AVAILABLE, "Worker is not available")
        if handoff["to_worker_id"] and handoff["to_worker_id"] != worker_id:
            return self._error(ERROR_TASK_SCOPE_VIOLATION, "Worker is not the handoff candidate")
        return None

    def _check_capabilities(self, conn, worker_id, task):
        raw = task["implementation_steps"] if "implementation_steps" in task.keys() else None
        reqs = []
        try:
            data = json.loads(raw) if raw else {}
            req = data.get("_requirements", {}) if isinstance(data, dict) else {}
            reqs = [str(v).lower() for v in req.values() if v]
        except Exception:
            reqs = []
        if not reqs:
            return None
        rows = conn.execute("SELECT capability FROM agent_capabilities WHERE worker_id=?", (worker_id,)).fetchall()
        caps = {row["capability"].lower() for row in rows}
        if any(req not in caps for req in reqs):
            return self._error(ERROR_WORKER_CAPABILITY_MISMATCH, "Worker capability mismatch")
        return None

    def _validate_request_inputs(self, task_id, assignment_id, worker_id, lease_token, reason_code, idem):
        if not isinstance(task_id, int) or task_id <= 0 or not assignment_id or not worker_id or not lease_token or not idem:
            return self._error(ERROR_VALIDATION_ERROR, "Required handoff fields are missing")
        if reason_code not in ALLOWED_REASON_CODES:
            return self._error(ERROR_HANDOFF_NOT_ALLOWED, "Invalid handoff reason_code")
        return None

    def _validate_packet(self, packet):
        if not packet["forbidden_actions"]:
            return self._error(ERROR_VALIDATION_ERROR, "forbidden_actions must be preserved")
        if self._contains_sensitive(packet):
            return self._error(ERROR_VALIDATION_ERROR, "Handoff packet contains sensitive data")
        for path in packet["files_changed"]:
            if not self._safe_relative_path(path):
                return self._error(ERROR_VALIDATION_ERROR, "Invalid file path in handoff packet")
        return None

    def _check_idempotency(self, conn, idempotency_key, fingerprint):
        row = conn.execute("SELECT * FROM task_handoffs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row is None:
            return None
        if row["request_fingerprint"] != fingerprint:
            return self._error(ERROR_IDEMPOTENCY_CONFLICT, "Idempotency key conflicts with prior request")
        return self._success(row["handoff_id"], row["task_id"], row["project_id"], row["status"], row["from_worker_id"], row["to_worker_id"], row["expires_at"], True)

    def _check_event_idempotency(self, conn, event_key, fingerprint):
        row = conn.execute("SELECT detail_json FROM task_events WHERE idempotency_key=?", (event_key,)).fetchone()
        if row is None:
            return None
        detail = self._loads(row["detail_json"], {})
        if detail.get("_fingerprint") != fingerprint:
            return self._error(ERROR_IDEMPOTENCY_CONFLICT, "Idempotency key conflicts with prior request")
        return {"success": True, "status": detail.get("status") or "idempotent", "handoff_id": detail.get("handoff_id"), "idempotent": True, "error_code": None}

    def _get_task(self, conn, task_id):
        return conn.execute("SELECT * FROM development_tasks WHERE id=?", (task_id,)).fetchone()

    def _get_handoff(self, conn, handoff_id):
        return conn.execute("SELECT * FROM task_handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()

    def _success(self, handoff_id, task_id, project_id, status, from_worker_id, to_worker_id, expires_at, idempotent):
        return {"success": True, "handoff_id": handoff_id, "task_id": task_id, "project_id": project_id, "status": status, "from_worker_id": from_worker_id, "to_worker_id": to_worker_id, "expires_at": expires_at, "idempotent": idempotent, "error_code": None}

    def _gate(self):
        if not self._v2_enabled:
            return self._error(ERROR_V2_CONTROL_PLANE_DISABLED, "V2 control plane is disabled")
        return None

    def _is_expired(self, value):
        if not value:
            return True
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S") <= datetime.now()
        except Exception:
            return True

    def _contains_sensitive(self, value):
        if isinstance(value, dict):
            for key, child in value.items():
                compact = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if compact in SENSITIVE_COMPACTS or any(s in compact for s in SENSITIVE_COMPACTS):
                    return True
                if self._contains_sensitive(child):
                    return True
        if isinstance(value, list):
            return any(self._contains_sensitive(item) for item in value)
        return False

    def _safe_relative_path(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            return False
        normalized = unquote(value).replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("/") or "://" in normalized:
            return False
        return ".." not in [part for part in normalized.split("/") if part]

    def _canonical_hash(self, value):
        return hashlib.sha256(self._json(value).encode("utf-8")).hexdigest()

    def _json(self, value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _loads(self, value, default):
        try:
            return json.loads(value) if isinstance(value, str) else value
        except Exception:
            return default

    def _now(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _rollback_close(self, conn):
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass

    def _error(self, code, message):
        return {"success": False, "error_code": code, "error_message": message, "idempotent": False}
