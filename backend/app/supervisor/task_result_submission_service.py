"""V2.0-B3a: task result submission and evidence packet persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote


ERROR_V2_CONTROL_PLANE_DISABLED = "V2_CONTROL_PLANE_DISABLED"
ERROR_VALIDATION_ERROR = "VALIDATION_ERROR"
ERROR_WORKER_NOT_REGISTERED = "WORKER_NOT_REGISTERED"
ERROR_WORKER_TYPE_NOT_ALLOWED = "WORKER_TYPE_NOT_ALLOWED"
ERROR_ASSIGNMENT_NOT_FOUND = "ASSIGNMENT_NOT_FOUND"
ERROR_TASK_SCOPE_VIOLATION = "TASK_SCOPE_VIOLATION"
ERROR_LEASE_CONFLICT = "LEASE_CONFLICT"
ERROR_STALE_LEASE = "STALE_LEASE"
ERROR_TASK_NOT_SUBMITTABLE = "TASK_NOT_SUBMITTABLE"
ERROR_STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
ERROR_RESULT_PACKET_INVALID = "RESULT_PACKET_INVALID"
ERROR_ARTIFACT_INVALID = "ARTIFACT_INVALID"
ERROR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"

WORKER_TYPE_EXECUTOR = "executor"
WORKER_STATUS_BUSY = "busy"
WORKER_STATUS_AVAILABLE = "available"
ACTIVE_ASSIGNMENT_STATUSES = {"assigned", "acknowledged", "running", "retrying"}
SUBMITTABLE_ASSIGNMENT_STATUSES = {"running"}
TASK_STATE_RUNNING = "running"
TASK_STATE_RESULT_SUBMITTED = "result_submitted"

REQUIRED_PACKET_FIELDS = {
    "execution_id",
    "result_status",
    "files_modified",
    "tests",
    "git_commit",
    "manual_actions",
    "errors",
    "evidence_refs",
    "handoff_requested",
    "remaining_steps",
    "worker_id",
    "submitted_at",
}

ALLOWED_ARTIFACT_TYPES = {
    "diff",
    "log",
    "test_report",
    "git_commit",
    "screenshot",
    "build_output",
    "lint_report",
    "coverage_report",
    "binary",
    "document",
    "other",
}

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "database_url",
    "db_password",
    "password",
    "secret",
    "token",
    "lease_token",
}
SENSITIVE_KEY_COMPACTS = {re.sub(r"[^a-z0-9]", "", key) for key in SENSITIVE_KEYS}

ARTIFACT_INLINE_PAYLOAD_KEYS = {
    "binary",
    "blob",
    "bytes",
    "content",
    "content_base64",
    "content_bytes",
    "data",
    "file_bytes",
    "payload",
}

MAX_ARTIFACT_URI_LENGTH = 512


class TaskResultSubmissionService:
    """Persist a Worker result packet and move RUNNING to RESULT_SUBMITTED.

    The mutation path uses one SQLite connection and one BEGIN IMMEDIATE
    transaction for result insert, artifact insert, task state update, event
    append, assignment completion, and conditional worker release.
    """

    def __init__(self, db_path: str, v2_enabled: Optional[bool] = None):
        self.db_path = db_path
        if v2_enabled is not None:
            self._v2_enabled = v2_enabled
        else:
            self._v2_enabled = os.getenv("V2_CONTROL_PLANE_ENABLED", "false").lower() in ("true", "1")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # Concurrent submitters can race while enabling WAL on a fresh temp DB.
            # The write path still uses BEGIN IMMEDIATE and busy_timeout.
            pass
        conn.row_factory = sqlite3.Row
        return conn

    def submit_result(
        self,
        task_id: int,
        assignment_id: str,
        worker_id: str,
        lease_token: str,
        expected_version: int,
        idempotency_key: str,
        result_packet: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not self._v2_enabled:
            return self._error(ERROR_V2_CONTROL_PLANE_DISABLED, "V2 control plane is disabled")

        if not isinstance(task_id, int) or task_id <= 0:
            return self._error(ERROR_VALIDATION_ERROR, "task_id is required")
        for name, value in {
            "assignment_id": assignment_id,
            "worker_id": worker_id,
            "lease_token": lease_token,
            "idempotency_key": idempotency_key,
        }.items():
            if not isinstance(value, str) or not value.strip():
                return self._error(ERROR_VALIDATION_ERROR, f"{name} is required")
        if not isinstance(expected_version, int) or expected_version <= 0:
            return self._error(ERROR_VALIDATION_ERROR, "expected_version must be positive")

        packet_result = self._validate_result_packet(
            task_id, assignment_id, worker_id, result_packet
        )
        if not packet_result["success"]:
            return packet_result
        packet = packet_result["packet"]
        artifacts = packet_result["artifacts"]
        fingerprint = self._request_fingerprint(
            task_id, assignment_id, worker_id, lease_token, expected_version, packet, artifacts
        )

        conn = self._get_conn()
        try:
            idem = self._check_idempotency(conn, idempotency_key, fingerprint)
            if idem is not None:
                conn.close()
                return idem

            worker = self._get_worker(conn, worker_id)
            if worker is None:
                conn.close()
                return self._error(ERROR_WORKER_NOT_REGISTERED, "Worker is not registered")
            if worker["worker_type"] != WORKER_TYPE_EXECUTOR:
                conn.close()
                return self._error(ERROR_WORKER_TYPE_NOT_ALLOWED, "Worker type is not allowed to submit results")

            assignment = self._get_assignment(conn, assignment_id)
            if assignment is None:
                conn.close()
                return self._error(ERROR_ASSIGNMENT_NOT_FOUND, "Assignment was not found")
            if int(assignment["task_id"]) != task_id:
                conn.close()
                return self._error(ERROR_TASK_SCOPE_VIOLATION, "Assignment does not belong to task")
            if assignment["worker_id"] != worker_id:
                conn.close()
                return self._error(ERROR_LEASE_CONFLICT, "Assignment belongs to another worker")
            if assignment["lease_token"] != lease_token:
                conn.close()
                return self._error(ERROR_LEASE_CONFLICT, "Lease token does not match")
            if assignment["status"] not in SUBMITTABLE_ASSIGNMENT_STATUSES:
                conn.close()
                return self._error(ERROR_TASK_NOT_SUBMITTABLE, "Assignment is not running")
            if self._is_expired(assignment["lease_expires_at"]):
                conn.close()
                return self._error(ERROR_STALE_LEASE, "Lease is expired")

            task = self._get_task(conn, task_id)
            if task is None:
                conn.close()
                return self._error(ERROR_TASK_NOT_SUBMITTABLE, "Task was not found")
            if (task["status"] or "").lower() != TASK_STATE_RUNNING:
                conn.close()
                return self._error(ERROR_TASK_NOT_SUBMITTABLE, "Task is not running")
            if int(task["state_version"] or 1) != expected_version:
                conn.close()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task state version changed")

            scope_error = self._validate_file_scope(task, packet["files_modified"])
            if scope_error:
                conn.close()
                return scope_error

            result = self._execute_submission_transaction(
                conn, task, assignment, worker_id, expected_version,
                idempotency_key, fingerprint, packet, artifacts
            )
            conn.close()
            return result
        except sqlite3.IntegrityError as exc:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
            return self._error(ERROR_INTERNAL_ERROR, str(exc))
        except Exception as exc:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
            return self._error(ERROR_INTERNAL_ERROR, str(exc))

    def _execute_submission_transaction(
        self,
        conn: sqlite3.Connection,
        task: sqlite3.Row,
        assignment: sqlite3.Row,
        worker_id: str,
        expected_version: int,
        idempotency_key: str,
        fingerprint: str,
        packet: Dict[str, Any],
        artifacts: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        cur = conn.cursor()
        task_id = int(task["id"])
        project_id = int(task["project_id"])
        assignment_id = assignment["assignment_id"]
        current_version = int(task["state_version"] or 1)
        new_version = current_version + 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result_id = f"rslt-{uuid.uuid4().hex[:16]}"
        event_id = f"event-{uuid.uuid4().hex[:16]}"

        conn.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("""
                SELECT status, state_version FROM development_tasks WHERE id = ?
            """, (task_id,))
            current_task = cur.fetchone()
            if current_task is None or (current_task["status"] or "").lower() != TASK_STATE_RUNNING:
                conn.rollback()
                return self._error(ERROR_TASK_NOT_SUBMITTABLE, "Task is not running")
            if int(current_task["state_version"] or 1) != expected_version:
                conn.rollback()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task state version changed")

            cur.execute("""
                SELECT status, lease_token, lease_expires_at, worker_id, task_id
                FROM task_assignments WHERE assignment_id = ?
            """, (assignment_id,))
            current_assignment = cur.fetchone()
            if current_assignment is None:
                conn.rollback()
                return self._error(ERROR_ASSIGNMENT_NOT_FOUND, "Assignment was not found")
            if (
                current_assignment["worker_id"] != worker_id
                or int(current_assignment["task_id"]) != task_id
                or current_assignment["lease_token"] != assignment["lease_token"]
            ):
                conn.rollback()
                return self._error(ERROR_LEASE_CONFLICT, "Lease changed")
            if current_assignment["status"] not in SUBMITTABLE_ASSIGNMENT_STATUSES:
                conn.rollback()
                return self._error(ERROR_TASK_NOT_SUBMITTABLE, "Assignment is not running")
            if self._is_expired(current_assignment["lease_expires_at"]):
                conn.rollback()
                return self._error(ERROR_STALE_LEASE, "Lease is expired")

            tests = packet["tests"]
            cur.execute("""
                INSERT INTO task_results
                (result_id, task_id, assignment_id, worker_id, project_id, result_status,
                 files_modified_json, files_checked_json, diff_summary,
                 tests_total, tests_passed, tests_failed, tests_skipped, test_output,
                 git_commit, git_branch, base_commit, exit_code, error_message,
                 stdout, stderr, model_calls, repair_attempts, duration_ms,
                 manual_actions_json, evidence_refs_json, handoff_requested,
                 remaining_steps_json, idempotency_key, submitted_at, completed_at)
                VALUES (?, ?, ?, ?, ?, 'submitted',
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?)
            """, (
                result_id, task_id, assignment_id, worker_id, project_id,
                self._json(packet["files_modified"]),
                self._json(packet.get("files_checked", [])),
                packet.get("diff_summary", ""),
                int(tests.get("total", 0)),
                int(tests.get("passed", 0)),
                int(tests.get("failed", 0)),
                int(tests.get("skipped", 0)),
                tests.get("output", ""),
                packet.get("git_commit", ""),
                packet.get("git_branch", ""),
                packet.get("base_commit", ""),
                packet.get("exit_code"),
                self._json(packet.get("errors", [])) if packet.get("errors") else "",
                packet.get("stdout", ""),
                packet.get("stderr", ""),
                int(packet.get("model_calls", 0) or 0),
                int(packet.get("repair_attempts", 0) or 0),
                int(packet.get("duration_ms", 0) or 0),
                self._json(packet["manual_actions"]),
                self._json(packet["evidence_refs"]),
                1 if packet["handoff_requested"] else 0,
                self._json(packet["remaining_steps"]),
                idempotency_key,
                packet["submitted_at"],
                now,
            ))

            for artifact in artifacts:
                cur.execute("""
                    INSERT INTO execution_artifacts
                    (artifact_id, result_id, task_id, assignment_id, project_id,
                     artifact_type, artifact_subtype, storage_path, storage_url,
                     content_hash, size_bytes, mime_type, description, tags_json,
                     is_sensitive, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    artifact["artifact_id"], result_id, task_id, assignment_id, project_id,
                    artifact["artifact_type"], artifact.get("artifact_subtype"),
                    artifact["uri"], None, artifact.get("sha256"),
                    artifact.get("size_bytes"), artifact.get("mime_type"),
                    artifact.get("description", ""),
                    self._json(artifact.get("tags", [])),
                    1 if artifact.get("is_sensitive") else 0,
                    self._json(artifact.get("metadata", {})),
                ))

            cur.execute("""
                UPDATE development_tasks
                SET status = ?, state_version = ?, last_state_change = ?
                WHERE id = ? AND status = ? AND state_version = ?
            """, (
                TASK_STATE_RESULT_SUBMITTED, new_version, now,
                task_id, TASK_STATE_RUNNING, expected_version,
            ))
            if cur.rowcount != 1:
                conn.rollback()
                return self._error(ERROR_STATE_VERSION_CONFLICT, "Task state version changed")

            detail = {
                "result_id": result_id,
                "assignment_id": assignment_id,
                "tests": {
                    "total": int(tests.get("total", 0)),
                    "passed": int(tests.get("passed", 0)),
                    "failed": int(tests.get("failed", 0)),
                    "skipped": int(tests.get("skipped", 0)),
                },
                "artifact_count": len(artifacts),
                "_fingerprint": fingerprint,
            }
            cur.execute("""
                INSERT INTO task_events
                (event_id, task_id, assignment_id, project_id, event_type,
                 from_state, to_state, reason, detail_json, operator_type, operator_id,
                 idempotency_key, state_version_before, state_version_after)
                VALUES (?, ?, ?, ?, 'submit', 'RUNNING', 'RESULT_SUBMITTED',
                        'Worker submitted result', ?, 'worker', ?, ?, ?, ?)
            """, (
                event_id, task_id, assignment_id, project_id,
                self._json(detail), worker_id, f"submit-event:{idempotency_key}",
                current_version, new_version,
            ))

            cur.execute("""
                UPDATE task_assignments
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE assignment_id = ? AND status = 'running'
            """, (now, now, assignment_id))
            if cur.rowcount != 1:
                conn.rollback()
                return self._error(ERROR_TASK_NOT_SUBMITTABLE, "Assignment is not running")

            cur.execute("""
                SELECT COUNT(*) AS c FROM task_assignments
                WHERE worker_id = ? AND status IN ('assigned','acknowledged','running','retrying')
            """, (worker_id,))
            active_count = int(cur.fetchone()["c"])
            worker_status = WORKER_STATUS_BUSY
            if active_count == 0:
                cur.execute("""
                    UPDATE agent_workers
                    SET status = ?, current_load = 0, last_seen_at = ?, version = version + 1
                    WHERE worker_id = ?
                """, (WORKER_STATUS_AVAILABLE, now, worker_id))
                if cur.rowcount != 1:
                    conn.rollback()
                    return self._error(ERROR_INTERNAL_ERROR, "Worker release failed")
                worker_status = WORKER_STATUS_AVAILABLE

            conn.commit()
            return {
                "success": True,
                "result_id": result_id,
                "task_id": task_id,
                "assignment_id": assignment_id,
                "task_state": "RESULT_SUBMITTED",
                "state_version": new_version,
                "assignment_status": "completed",
                "worker_status": worker_status,
                "artifact_count": len(artifacts),
                "idempotent": False,
                "result_summary": self._result_summary(packet),
                "error_code": None,
                "error_message": None,
            }
        except Exception:
            conn.rollback()
            raise

    def _check_idempotency(
        self, conn: sqlite3.Connection, idempotency_key: str, fingerprint: str
    ) -> Optional[Dict[str, Any]]:
        row = conn.execute("""
            SELECT result_id, task_id, assignment_id, evidence_refs_json
            FROM task_results WHERE idempotency_key = ?
        """, (idempotency_key,)).fetchone()
        if row is None:
            return None
        event = conn.execute("""
            SELECT detail_json, state_version_after
            FROM task_events
            WHERE idempotency_key = ? AND event_type = 'submit'
        """, (f"submit-event:{idempotency_key}",)).fetchone()
        if event is None:
            return self._error(ERROR_IDEMPOTENCY_CONFLICT, "Idempotency record is incomplete")
        detail = self._loads(event["detail_json"], {})
        if detail.get("_fingerprint") != fingerprint:
            return self._error(ERROR_IDEMPOTENCY_CONFLICT, "Idempotency key conflicts with prior request")
        artifacts = conn.execute("""
            SELECT COUNT(*) AS c FROM execution_artifacts WHERE result_id = ?
        """, (row["result_id"],)).fetchone()
        worker = conn.execute("""
            SELECT status FROM agent_workers
            WHERE worker_id = (
                SELECT worker_id FROM task_results WHERE result_id = ?
            )
        """, (row["result_id"],)).fetchone()
        return {
            "success": True,
            "result_id": row["result_id"],
            "task_id": row["task_id"],
            "assignment_id": row["assignment_id"],
            "task_state": "RESULT_SUBMITTED",
            "state_version": event["state_version_after"],
            "assignment_status": "completed",
            "worker_status": worker["status"] if worker else WORKER_STATUS_AVAILABLE,
            "artifact_count": int(artifacts["c"]),
            "idempotent": True,
            "result_summary": {"evidence_refs": self._loads(row["evidence_refs_json"], [])},
            "error_code": None,
            "error_message": None,
        }

    def _validate_result_packet(
        self, task_id: int, assignment_id: str, worker_id: str, packet: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not isinstance(packet, dict):
            return self._error(ERROR_RESULT_PACKET_INVALID, "Result packet must be an object")
        missing = sorted(REQUIRED_PACKET_FIELDS - set(packet))
        if missing:
            return self._error(ERROR_RESULT_PACKET_INVALID, "Result packet is missing required fields")
        if packet.get("task_id", task_id) != task_id:
            return self._error(ERROR_TASK_SCOPE_VIOLATION, "Packet task_id does not match URL task_id")
        if packet.get("assignment_id", assignment_id) != assignment_id:
            return self._error(ERROR_TASK_SCOPE_VIOLATION, "Packet assignment_id does not match request")
        if packet.get("worker_id") != worker_id:
            return self._error(ERROR_LEASE_CONFLICT, "Packet worker_id does not match request")
        if packet.get("result_status") != "submitted":
            return self._error(ERROR_RESULT_PACKET_INVALID, "Worker may only submit result_status=submitted")
        if self._contains_sensitive_key(packet):
            return self._error(ERROR_RESULT_PACKET_INVALID, "Result packet contains sensitive fields")

        tests = packet.get("tests")
        if not isinstance(tests, dict):
            return self._error(ERROR_RESULT_PACKET_INVALID, "tests must be an object")
        for key in ("total", "passed", "failed", "skipped"):
            if not isinstance(tests.get(key), int) or tests.get(key) < 0:
                return self._error(ERROR_RESULT_PACKET_INVALID, "test counters must be non-negative integers")
        if tests["passed"] + tests["failed"] + tests["skipped"] > tests["total"]:
            return self._error(ERROR_RESULT_PACKET_INVALID, "test counters exceed total")

        for key in ("files_modified", "manual_actions", "errors", "evidence_refs", "remaining_steps"):
            if not isinstance(packet.get(key), list):
                return self._error(ERROR_RESULT_PACKET_INVALID, f"{key} must be a list")
        if not isinstance(packet.get("handoff_requested"), bool):
            return self._error(ERROR_RESULT_PACKET_INVALID, "handoff_requested must be boolean")

        for file_path in packet["files_modified"] + packet.get("files_checked", []):
            if not self._is_safe_relative_path(file_path):
                return self._error(ERROR_RESULT_PACKET_INVALID, "File path is outside allowed scope")

        git_commit = packet.get("git_commit", "")
        if git_commit and not re.fullmatch(r"[0-9a-fA-F]{7,40}", git_commit):
            return self._error(ERROR_RESULT_PACKET_INVALID, "git_commit format is invalid")
        if not git_commit and int(tests.get("failed", 0)) == 0 and int(packet.get("exit_code", 0) or 0) == 0:
            return self._error(ERROR_RESULT_PACKET_INVALID, "git_commit is required for successful results")

        artifacts = packet.get("artifacts", [])
        artifact_result = self._validate_artifacts(artifacts, packet["evidence_refs"])
        if not artifact_result["success"]:
            return artifact_result

        normalized = dict(packet)
        normalized.setdefault("assignment_id", assignment_id)
        normalized.setdefault("task_id", task_id)
        normalized.setdefault("project_id", None)
        normalized.setdefault("files_checked", [])
        normalized.setdefault("diff_summary", "")
        normalized.setdefault("git_branch", "")
        normalized.setdefault("base_commit", "")
        normalized.setdefault("stdout", "")
        normalized.setdefault("stderr", "")
        normalized.setdefault("exit_code", 0)
        normalized.setdefault("duration_ms", 0)
        normalized.setdefault("model_calls", 0)
        normalized.setdefault("repair_attempts", 0)
        return {"success": True, "packet": normalized, "artifacts": artifact_result["artifacts"]}

    def _validate_artifacts(self, artifacts: Any, evidence_refs: List[str]) -> Dict[str, Any]:
        if artifacts is None:
            artifacts = []
        if not isinstance(artifacts, list):
            return self._error(ERROR_ARTIFACT_INVALID, "artifacts must be a list")
        refs = set()
        for ref in evidence_refs:
            if not isinstance(ref, str) or not ref.strip():
                return self._error(ERROR_ARTIFACT_INVALID, "evidence_refs must contain artifact ids")
            refs.add(ref)

        normalized = []
        artifact_ids = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                return self._error(ERROR_ARTIFACT_INVALID, "artifact must be an object")
            if self._contains_sensitive_key(artifact):
                return self._error(ERROR_ARTIFACT_INVALID, "Artifact contains sensitive fields")
            if self._contains_inline_payload_key(artifact):
                return self._error(ERROR_ARTIFACT_INVALID, "Artifact must store metadata only")
            artifact_id = artifact.get("artifact_id")
            artifact_type = artifact.get("artifact_type")
            uri = artifact.get("uri") or artifact.get("path")
            sha256 = artifact.get("sha256")
            size_bytes = artifact.get("size_bytes")
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                return self._error(ERROR_ARTIFACT_INVALID, "artifact_id is required")
            if artifact_id in artifact_ids:
                return self._error(ERROR_ARTIFACT_INVALID, "duplicate artifact_id")
            artifact_ids.add(artifact_id)
            if artifact_type not in ALLOWED_ARTIFACT_TYPES:
                return self._error(ERROR_ARTIFACT_INVALID, "artifact_type is invalid")
            if not self._is_safe_relative_path(uri):
                return self._error(ERROR_ARTIFACT_INVALID, "artifact uri is invalid")
            if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", str(sha256)):
                return self._error(ERROR_ARTIFACT_INVALID, "artifact sha256 is invalid")
            if size_bytes is not None and (not isinstance(size_bytes, int) or size_bytes < 0):
                return self._error(ERROR_ARTIFACT_INVALID, "artifact size_bytes is invalid")
            normalized.append({
                **artifact,
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "uri": uri,
            })

        if refs - artifact_ids:
            return self._error(ERROR_ARTIFACT_INVALID, "evidence_refs must reference supplied artifacts")
        return {"success": True, "artifacts": normalized}

    def _validate_file_scope(self, task: sqlite3.Row, files_modified: List[str]) -> Optional[Dict[str, Any]]:
        allowed = set()
        for column in ("files_to_modify", "files_to_check"):
            raw = task[column] if column in task.keys() else None
            parsed = self._loads(raw, [])
            if isinstance(parsed, list):
                allowed.update(str(p).replace("\\", "/") for p in parsed)
        if allowed:
            for file_path in files_modified:
                if str(file_path).replace("\\", "/") not in allowed:
                    return self._error(ERROR_RESULT_PACKET_INVALID, "files_modified contains a file outside allowed scope")
        return None

    def _get_worker(self, conn: sqlite3.Connection, worker_id: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT worker_id, worker_type, status FROM agent_workers WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()

    def _get_assignment(self, conn: sqlite3.Connection, assignment_id: str) -> Optional[sqlite3.Row]:
        return conn.execute("""
            SELECT assignment_id, task_id, worker_id, project_id, status, lease_token, lease_expires_at
            FROM task_assignments WHERE assignment_id = ?
        """, (assignment_id,)).fetchone()

    def _get_task(self, conn: sqlite3.Connection, task_id: int) -> Optional[sqlite3.Row]:
        return conn.execute("""
            SELECT id, project_id, status, state_version, files_to_modify, files_to_check
            FROM development_tasks WHERE id = ?
        """, (task_id,)).fetchone()

    def _request_fingerprint(
        self,
        task_id: int,
        assignment_id: str,
        worker_id: str,
        lease_token: str,
        expected_version: int,
        packet: Dict[str, Any],
        artifacts: List[Dict[str, Any]],
    ) -> str:
        payload = {
            "task_id": task_id,
            "assignment_id": assignment_id,
            "worker_id": worker_id,
            "lease_token_hash": hashlib.sha256(lease_token.encode()).hexdigest(),
            "expected_version": expected_version,
            "result_packet_hash": self._canonical_hash(packet),
            "artifact_hash": self._canonical_hash(artifacts),
        }
        return self._canonical_hash(payload)

    def _canonical_hash(self, value: Any) -> str:
        return hashlib.sha256(self._json(value).encode("utf-8")).hexdigest()

    def _result_summary(self, packet: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "result_status": packet["result_status"],
            "tests": {
                "total": packet["tests"]["total"],
                "passed": packet["tests"]["passed"],
                "failed": packet["tests"]["failed"],
                "skipped": packet["tests"]["skipped"],
            },
            "files_modified": packet["files_modified"],
            "evidence_refs": packet["evidence_refs"],
            "handoff_requested": packet["handoff_requested"],
        }

    def _is_expired(self, lease_expires_at: Optional[str]) -> bool:
        if not lease_expires_at:
            return True
        try:
            return datetime.fromisoformat(str(lease_expires_at).replace("Z", "+00:00")).replace(tzinfo=None) <= datetime.now()
        except ValueError:
            try:
                return datetime.strptime(str(lease_expires_at), "%Y-%m-%d %H:%M:%S") <= datetime.now()
            except ValueError:
                return True

    def _is_safe_relative_path(self, value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        if len(value) > MAX_ARTIFACT_URI_LENGTH:
            return False
        normalized = unquote(value).replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("/"):
            return False
        if "://" in normalized:
            return False
        parts = [part for part in normalized.split("/") if part]
        return ".." not in parts

    def _contains_sensitive_key(self, value: Any) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                compact = re.sub(r"[^a-z0-9]", "", lowered)
                if (
                    lowered in SENSITIVE_KEYS
                    or compact in SENSITIVE_KEY_COMPACTS
                    or any(sensitive in compact for sensitive in SENSITIVE_KEY_COMPACTS)
                    or lowered.endswith("_token")
                    or lowered.endswith("_secret")
                    or compact.endswith("token")
                    or compact.endswith("secret")
                ):
                    return True
                if self._contains_sensitive_key(child):
                    return True
        elif isinstance(value, list):
            return any(self._contains_sensitive_key(item) for item in value)
        return False

    def _contains_inline_payload_key(self, value: Dict[str, Any]) -> bool:
        for key in value:
            lowered = str(key).lower()
            compact = re.sub(r"[^a-z0-9]", "_", lowered).strip("_")
            if compact in ARTIFACT_INLINE_PAYLOAD_KEYS:
                return True
        return False

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _loads(self, value: Any, default: Any) -> Any:
        if value is None:
            return default
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except Exception:
            return default

    def _error(self, code: str, message: str) -> Dict[str, Any]:
        return {
            "success": False,
            "error_code": code,
            "error_message": message,
            "idempotent": False,
        }
