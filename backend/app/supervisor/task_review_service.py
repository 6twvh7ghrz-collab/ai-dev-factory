"""V2.0-B3b: reviewer decisions and review state transitions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional


ERROR_V2_CONTROL_PLANE_DISABLED = "V2_CONTROL_PLANE_DISABLED"
ERROR_VALIDATION_ERROR = "VALIDATION_ERROR"
ERROR_REVIEWER_NOT_REGISTERED = "REVIEWER_NOT_REGISTERED"
ERROR_REVIEWER_NOT_AVAILABLE = "REVIEWER_NOT_AVAILABLE"
ERROR_REVIEWER_TYPE_NOT_ALLOWED = "REVIEWER_TYPE_NOT_ALLOWED"
ERROR_RESULT_NOT_FOUND = "RESULT_NOT_FOUND"
ERROR_RESULT_NOT_REVIEWABLE = "RESULT_NOT_REVIEWABLE"
ERROR_TASK_NOT_REVIEWABLE = "TASK_NOT_REVIEWABLE"
ERROR_TASK_SCOPE_VIOLATION = "TASK_SCOPE_VIOLATION"
ERROR_STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
ERROR_REVIEW_CONFLICT = "REVIEW_CONFLICT"
ERROR_EVIDENCE_INVALID = "EVIDENCE_INVALID"
ERROR_DECISION_INVALID = "DECISION_INVALID"
ERROR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"

TASK_RESULT_SUBMITTED = "result_submitted"
TASK_REVIEWING = "reviewing"
FINAL_TASK_STATES = {
    "VERIFIED": "verified",
    "REWORK": "rework",
    "BLOCKED": "blocked",
    "NEED_USER": "need_user",
}
FINAL_DECISIONS = set(FINAL_TASK_STATES)
SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "database_url", "password", "secret", "token", "lease_token"}
SENSITIVE_KEY_COMPACTS = {re.sub(r"[^a-z0-9]", "", key) for key in SENSITIVE_KEYS}


class TaskReviewService:
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

    def begin_review(
        self,
        task_id: int,
        result_id: str,
        reviewer_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if not self._v2_enabled:
            return self._error(ERROR_V2_CONTROL_PLANE_DISABLED, "V2 control plane is disabled")
        basic = self._validate_common(task_id, result_id, reviewer_id, expected_version, idempotency_key)
        if basic:
            return basic
        fingerprint = self._canonical_hash({
            "op": "begin",
            "task_id": task_id,
            "result_id": result_id,
            "reviewer_id": reviewer_id,
            "expected_version": expected_version,
        })
        conn = self._get_conn()
        try:
            idem = self._check_idempotency(conn, f"review-begin:{idempotency_key}", fingerprint)
            if idem is not None:
                conn.close()
                return idem
            pre = self._preflight(conn, task_id, result_id, reviewer_id, expected_version, TASK_RESULT_SUBMITTED)
            if not pre["success"]:
                conn.close()
                return pre
            result = self._execute_begin(conn, pre["task"], pre["result"], reviewer_id, idempotency_key, fingerprint)
            conn.close()
            return result
        except Exception as exc:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
            return self._error(ERROR_INTERNAL_ERROR, str(exc))

    def submit_decision(
        self,
        task_id: int,
        result_id: str,
        reviewer_id: str,
        expected_version: int,
        decision: str,
        summary: str,
        issues: List[Dict[str, Any]],
        evidence_refs: List[str],
        idempotency_key: str,
        risk_level: str = "low",
        user_action_required: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self._v2_enabled:
            return self._error(ERROR_V2_CONTROL_PLANE_DISABLED, "V2 control plane is disabled")
        basic = self._validate_common(task_id, result_id, reviewer_id, expected_version, idempotency_key)
        if basic:
            return basic
        decision = (decision or "").upper()
        if decision not in FINAL_DECISIONS:
            return self._error(ERROR_DECISION_INVALID, "Decision is invalid")
        if self._contains_sensitive_key(metadata or {}):
            return self._error(ERROR_DECISION_INVALID, "Decision metadata contains sensitive fields")
        packet_error = self._validate_decision_packet(decision, summary, issues, evidence_refs)
        if packet_error:
            return packet_error
        fingerprint = self._canonical_hash({
            "op": "decision",
            "task_id": task_id,
            "result_id": result_id,
            "reviewer_id": reviewer_id,
            "expected_version": expected_version,
            "decision": decision,
            "summary": summary,
            "issues_hash": self._canonical_hash(issues),
            "evidence_refs_hash": self._canonical_hash(evidence_refs),
        })
        conn = self._get_conn()
        try:
            idem = self._check_idempotency(conn, f"review-decision:{idempotency_key}", fingerprint)
            if idem is not None:
                conn.close()
                return idem
            pre = self._preflight(conn, task_id, result_id, reviewer_id, expected_version, TASK_REVIEWING)
            if not pre["success"]:
                conn.close()
                return pre
            evidence_error = self._validate_evidence(conn, result_id, evidence_refs)
            if evidence_error:
                conn.close()
                return evidence_error
            result_error = self._validate_result_for_decision(pre["result"], decision, summary, issues)
            if result_error:
                conn.close()
                return result_error
            result = self._execute_decision(
                conn, pre["task"], pre["result"], reviewer_id, idempotency_key, fingerprint,
                decision, summary, issues, evidence_refs, risk_level, user_action_required, metadata or {}
            )
            conn.close()
            return result
        except Exception as exc:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
            return self._error(ERROR_INTERNAL_ERROR, str(exc))

    def _execute_begin(self, conn, task, result, reviewer_id, idempotency_key, fingerprint):
        cur = conn.cursor()
        now = self._now()
        review_id = f"rvw-{uuid.uuid4().hex[:16]}"
        event_id = f"event-{uuid.uuid4().hex[:16]}"
        task_id = int(task["id"])
        result_id = result["result_id"]
        project_id = int(task["project_id"])
        current_version = int(task["state_version"])
        new_version = current_version + 1
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = cur.execute("SELECT status, state_version FROM development_tasks WHERE id=?", (task_id,)).fetchone()
            if current["status"] != TASK_RESULT_SUBMITTED or int(current["state_version"]) != current_version:
                conn.rollback()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task changed")
            existing = cur.execute("SELECT * FROM review_decisions WHERE result_id=?", (result_id,)).fetchone()
            if existing and existing["decision"] != "REVIEWING":
                conn.rollback()
                return self._error(ERROR_REVIEW_CONFLICT, "Result already reviewed")
            if existing and existing["reviewer_id"] != reviewer_id:
                conn.rollback()
                return self._error(ERROR_REVIEW_CONFLICT, "Another reviewer is reviewing this result")
            if existing is None:
                cur.execute("""
                    INSERT INTO review_decisions
                    (review_id, result_id, task_id, project_id, reviewer_type, reviewer_id,
                     decision, summary, issues_json, evidence_refs_json, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'reviewer', ?, 'REVIEWING', '', '[]', '[]', '{}', ?, ?)
                """, (review_id, result_id, task_id, project_id, reviewer_id, now, now))
            else:
                review_id = existing["review_id"]
                cur.execute("UPDATE review_decisions SET updated_at=? WHERE review_id=?", (now, review_id))
            cur.execute("""
                UPDATE development_tasks
                SET status='reviewing', state_version=?, last_state_change=?
                WHERE id=? AND status='result_submitted' AND state_version=?
            """, (new_version, now, task_id, current_version))
            if cur.rowcount != 1:
                conn.rollback()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task changed")
            detail = {
                "review_id": review_id,
                "task_id": task_id,
                "result_id": result_id,
                "reviewer_id": reviewer_id,
                "from_state": "RESULT_SUBMITTED",
                "to_state": "REVIEWING",
                "_fingerprint": fingerprint,
            }
            cur.execute("""
                INSERT INTO task_events
                (event_id, task_id, assignment_id, project_id, event_type, from_state, to_state,
                 reason, detail_json, operator_type, operator_id, idempotency_key,
                 state_version_before, state_version_after)
                VALUES (?, ?, ?, ?, 'review', 'RESULT_SUBMITTED', 'REVIEWING',
                        'Reviewer began review', ?, 'reviewer', ?, ?, ?, ?)
            """, (
                event_id, task_id, result["assignment_id"], project_id, self._json(detail),
                reviewer_id, f"review-begin:{idempotency_key}", current_version, new_version,
            ))
            conn.commit()
            return self._success(review_id, task_id, result_id, reviewer_id, "RESULT_SUBMITTED", "REVIEWING", new_version, "REVIEWING", "", False)
        except Exception:
            conn.rollback()
            raise

    def _execute_decision(
        self, conn, task, result, reviewer_id, idempotency_key, fingerprint,
        decision, summary, issues, evidence_refs, risk_level, user_action_required, metadata
    ):
        cur = conn.cursor()
        now = self._now()
        event_id = f"event-{uuid.uuid4().hex[:16]}"
        task_id = int(task["id"])
        result_id = result["result_id"]
        project_id = int(task["project_id"])
        current_version = int(task["state_version"])
        new_version = current_version + 1
        target_state = FINAL_TASK_STATES[decision]
        conn.execute("BEGIN IMMEDIATE")
        try:
            review = cur.execute("SELECT * FROM review_decisions WHERE result_id=?", (result_id,)).fetchone()
            if review is None or review["decision"] != "REVIEWING":
                conn.rollback()
                return self._error(ERROR_REVIEW_CONFLICT, "Result is not under review")
            if review["reviewer_id"] != reviewer_id:
                conn.rollback()
                return self._error(ERROR_REVIEW_CONFLICT, "Another reviewer owns this review")
            current = cur.execute("SELECT status, state_version FROM development_tasks WHERE id=?", (task_id,)).fetchone()
            if current["status"] != TASK_REVIEWING or int(current["state_version"]) != current_version:
                conn.rollback()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task changed")
            cur.execute("""
                UPDATE review_decisions
                SET decision=?, summary=?, reason=?, issues_json=?, evidence_refs_json=?,
                    risk_level=?, user_action_required=?, metadata_json=?, updated_at=?
                WHERE review_id=? AND decision='REVIEWING'
            """, (
                decision, summary, summary, self._json(issues), self._json(evidence_refs),
                risk_level, 1 if user_action_required else 0, self._json(metadata),
                now, review["review_id"],
            ))
            if cur.rowcount != 1:
                conn.rollback()
                return self._error(ERROR_REVIEW_CONFLICT, "Review changed")
            cur.execute("""
                UPDATE development_tasks
                SET status=?, state_version=?, last_state_change=?
                WHERE id=? AND status='reviewing' AND state_version=?
            """, (target_state, new_version, now, task_id, current_version))
            if cur.rowcount != 1:
                conn.rollback()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task changed")
            detail = {
                "review_id": review["review_id"],
                "task_id": task_id,
                "result_id": result_id,
                "reviewer_id": reviewer_id,
                "from_state": "REVIEWING",
                "to_state": decision,
                "decision": decision,
                "summary": summary,
                "issues": issues,
                "evidence_refs": evidence_refs,
                "_fingerprint": fingerprint,
            }
            cur.execute("""
                INSERT INTO task_events
                (event_id, task_id, assignment_id, project_id, event_type, from_state, to_state,
                 reason, detail_json, operator_type, operator_id, idempotency_key,
                 state_version_before, state_version_after)
                VALUES (?, ?, ?, ?, 'review', 'REVIEWING', ?, ?, ?, 'reviewer', ?, ?, ?, ?)
            """, (
                event_id, task_id, result["assignment_id"], project_id, decision,
                summary, self._json(detail), reviewer_id, f"review-decision:{idempotency_key}",
                current_version, new_version,
            ))
            conn.commit()
            return self._success(review["review_id"], task_id, result_id, reviewer_id, "REVIEWING", decision, new_version, decision, summary, False)
        except Exception:
            conn.rollback()
            raise

    def _preflight(self, conn, task_id, result_id, reviewer_id, expected_version, expected_task_status):
        reviewer = conn.execute("SELECT worker_id, worker_type, status FROM agent_workers WHERE worker_id=?", (reviewer_id,)).fetchone()
        if reviewer is None:
            return self._error(ERROR_REVIEWER_NOT_REGISTERED, "Reviewer is not registered")
        if reviewer["worker_type"] != "reviewer":
            return self._error(ERROR_REVIEWER_TYPE_NOT_ALLOWED, "Only reviewer workers may review")
        if reviewer["status"] in ("offline", "disabled"):
            return self._error(ERROR_REVIEWER_NOT_AVAILABLE, "Reviewer is not available")
        result = conn.execute("SELECT * FROM task_results WHERE result_id=?", (result_id,)).fetchone()
        if result is None:
            return self._error(ERROR_RESULT_NOT_FOUND, "Result was not found")
        if int(result["task_id"]) != task_id:
            return self._error(ERROR_TASK_SCOPE_VIOLATION, "Result does not belong to task")
        if result["worker_id"] == reviewer_id:
            return self._error(ERROR_REVIEWER_TYPE_NOT_ALLOWED, "Reviewer cannot review its own result")
        task = conn.execute("SELECT id, project_id, status, state_version FROM development_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None or int(task["project_id"]) != int(result["project_id"]):
            return self._error(ERROR_TASK_SCOPE_VIOLATION, "Task/result project mismatch")
        if task["status"] != expected_task_status:
            return self._error(ERROR_TASK_NOT_REVIEWABLE, "Task is not reviewable")
        if int(task["state_version"]) != expected_version:
            return self._error(ERROR_STATE_VERSION_CONFLICT, "Task state version changed")
        current_review = conn.execute("SELECT * FROM review_decisions WHERE result_id=?", (result_id,)).fetchone()
        if expected_task_status == TASK_RESULT_SUBMITTED and current_review and current_review["reviewer_id"] != reviewer_id:
            return self._error(ERROR_REVIEW_CONFLICT, "Another reviewer is reviewing this result")
        if expected_task_status == TASK_REVIEWING:
            if current_review is None or current_review["decision"] != "REVIEWING":
                return self._error(ERROR_RESULT_NOT_REVIEWABLE, "Result is not in review")
            if current_review["reviewer_id"] != reviewer_id:
                return self._error(ERROR_REVIEW_CONFLICT, "Another reviewer owns this review")
        return {"success": True, "task": task, "result": result}

    def _validate_common(self, task_id, result_id, reviewer_id, expected_version, idempotency_key):
        if not isinstance(task_id, int) or task_id <= 0:
            return self._error(ERROR_VALIDATION_ERROR, "task_id is required")
        for value_name, value in {"result_id": result_id, "reviewer_id": reviewer_id, "idempotency_key": idempotency_key}.items():
            if not isinstance(value, str) or not value.strip():
                return self._error(ERROR_VALIDATION_ERROR, f"{value_name} is required")
        if not isinstance(expected_version, int) or expected_version <= 0:
            return self._error(ERROR_VALIDATION_ERROR, "expected_version must be positive")
        return None

    def _validate_decision_packet(self, decision, summary, issues, evidence_refs):
        if not isinstance(summary, str) or not summary.strip():
            return self._error(ERROR_DECISION_INVALID, "summary is required")
        if not isinstance(issues, list) or not isinstance(evidence_refs, list):
            return self._error(ERROR_DECISION_INVALID, "issues and evidence_refs must be lists")
        if decision == "REWORK":
            if not issues:
                return self._error(ERROR_DECISION_INVALID, "REWORK requires issues")
            for issue in issues:
                if not isinstance(issue, dict) or not issue.get("severity") or not issue.get("reason") or not (issue.get("acceptance") or issue.get("suggested_fix")):
                    return self._error(ERROR_DECISION_INVALID, "Each REWORK issue needs severity, reason, and fix/acceptance")
        if decision == "BLOCKED" and not issues:
            return self._error(ERROR_DECISION_INVALID, "BLOCKED requires a blocking reason")
        if decision == "NEED_USER":
            if not issues:
                return self._error(ERROR_DECISION_INVALID, "NEED_USER requires a user question")
            first = issues[0]
            if not isinstance(first, dict) or not first.get("question") or not first.get("options") or not first.get("risk"):
                return self._error(ERROR_DECISION_INVALID, "NEED_USER requires question, options, and risk")
        return None

    def _validate_evidence(self, conn, result_id, evidence_refs):
        refs = set()
        for ref in evidence_refs:
            if not isinstance(ref, str) or not ref.strip() or ".." in ref or "/" in ref or "\\" in ref:
                return self._error(ERROR_EVIDENCE_INVALID, "Evidence reference is invalid")
            refs.add(ref)
        if not refs:
            return self._error(ERROR_EVIDENCE_INVALID, "Evidence references are required")
        rows = conn.execute("SELECT artifact_id FROM execution_artifacts WHERE result_id=?", (result_id,)).fetchall()
        available = {row["artifact_id"] for row in rows}
        if not refs.issubset(available):
            return self._error(ERROR_EVIDENCE_INVALID, "Evidence reference does not exist")
        return None

    def _validate_result_for_decision(self, result, decision, summary, issues):
        if decision == "VERIFIED":
            failed = int(result["tests_failed"] or 0)
            if failed > 0:
                return self._error(ERROR_DECISION_INVALID, "VERIFIED requires zero failed tests")
        return None

    def _check_idempotency(self, conn, event_key, fingerprint):
        row = conn.execute("SELECT detail_json, state_version_after FROM task_events WHERE idempotency_key=?", (event_key,)).fetchone()
        if row is None:
            return None
        detail = self._loads(row["detail_json"], {})
        if detail.get("_fingerprint") != fingerprint:
            return self._error(ERROR_IDEMPOTENCY_CONFLICT, "Idempotency key conflicts with prior request")
        return self._success(
            detail.get("review_id"), detail.get("task_id") or 0, detail.get("result_id"),
            detail.get("reviewer_id", ""), detail.get("from_state", ""), detail.get("decision") or detail.get("to_state", "REVIEWING"),
            row["state_version_after"], detail.get("decision") or detail.get("to_state", "REVIEWING"),
            detail.get("summary", ""), True,
        )

    def _success(self, review_id, task_id, result_id, reviewer_id, previous_state, task_state, version, decision, summary, idempotent):
        return {
            "success": True,
            "decision_id": review_id,
            "task_id": task_id,
            "result_id": result_id,
            "reviewer_id": reviewer_id,
            "previous_state": previous_state,
            "task_state": task_state,
            "state_version": version,
            "decision": decision,
            "summary": summary,
            "idempotent": idempotent,
            "error_code": None,
            "error_message": None,
        }

    def _canonical_hash(self, value: Any) -> str:
        return hashlib.sha256(self._json(value).encode("utf-8")).hexdigest()

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _loads(self, value: Any, default: Any) -> Any:
        try:
            return json.loads(value) if isinstance(value, str) else value
        except Exception:
            return default

    def _contains_sensitive_key(self, value: Any) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                compact = re.sub(r"[^a-z0-9]", "", lowered)
                if lowered in SENSITIVE_KEYS or compact in SENSITIVE_KEY_COMPACTS or any(s in compact for s in SENSITIVE_KEY_COMPACTS):
                    return True
                if self._contains_sensitive_key(child):
                    return True
        elif isinstance(value, list):
            return any(self._contains_sensitive_key(item) for item in value)
        return False

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _error(self, code: str, message: str) -> Dict[str, Any]:
        return {"success": False, "error_code": code, "error_message": message, "idempotent": False}
