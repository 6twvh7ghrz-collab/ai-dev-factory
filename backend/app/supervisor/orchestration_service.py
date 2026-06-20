"""V2.0-B5a: deterministic supervisor orchestration.

The service performs one control-plane decision per cycle. It does not call AI,
start Executors, run project tasks, or write task state directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .task_claim_service import TaskClaimService
from .task_review_service import TaskReviewService


ERROR_V2_CONTROL_PLANE_DISABLED = "V2_CONTROL_PLANE_DISABLED"
ERROR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"
ERROR_VALIDATION_ERROR = "VALIDATION_ERROR"

STALE_HEARTBEAT_SECONDS = 300
TERMINAL_STATES = {"verified", "cancelled", "failed"}
WAIT_STATES = {"claimed", "running", "reviewing"}


class SupervisorOrchestrationService:
    def __init__(self, db_path: str, v2_enabled: Optional[bool] = None, stale_heartbeat_seconds: int = STALE_HEARTBEAT_SECONDS):
        self.db_path = db_path
        self.stale_heartbeat_seconds = stale_heartbeat_seconds
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

    def inspect_project(self, project_id: int) -> Dict[str, Any]:
        if not self._v2_enabled:
            return self._error(ERROR_V2_CONTROL_PLANE_DISABLED, "V2 control plane is disabled")
        conn = self._get_conn()
        try:
            tasks = [dict(r) for r in conn.execute(
                "SELECT id, project_id, status, state_version, implementation_steps FROM development_tasks WHERE project_id=? ORDER BY id",
                (project_id,),
            ).fetchall()]
            workers = [self._safe_worker(dict(r)) for r in conn.execute(
                "SELECT worker_id, worker_type, status, current_load, max_concurrency, last_seen_at FROM agent_workers ORDER BY worker_id"
            ).fetchall()]
            return {"success": True, "project_id": project_id, "tasks": tasks, "workers": workers, "error_code": None}
        except Exception as exc:
            return self._error(ERROR_INTERNAL_ERROR, str(exc))
        finally:
            conn.close()

    def plan_next_action(self, project_id: int, idempotency_key: str) -> Dict[str, Any]:
        if not self._v2_enabled:
            return self._error(ERROR_V2_CONTROL_PLANE_DISABLED, "V2 control plane is disabled")
        if not idempotency_key:
            return self._error(ERROR_VALIDATION_ERROR, "idempotency_key is required")
        conn = self._get_conn()
        try:
            return self._plan(conn, project_id)
        except Exception as exc:
            return self._error(ERROR_INTERNAL_ERROR, str(exc))
        finally:
            conn.close()

    def run_one_cycle(self, project_id: int, idempotency_key: str, dry_run: bool = True) -> Dict[str, Any]:
        if not self._v2_enabled:
            return self._error(ERROR_V2_CONTROL_PLANE_DISABLED, "V2 control plane is disabled")
        if not idempotency_key:
            return self._error(ERROR_VALIDATION_ERROR, "idempotency_key is required")

        conn = self._get_conn()
        try:
            if not dry_run:
                existing = self._cycle_by_key(conn, idempotency_key)
                if existing is not None:
                    return existing
            plan = self._plan(conn, project_id)
            if not plan.get("success"):
                return plan
            fingerprint = self._fingerprint(project_id, plan, dry_run)
            if dry_run:
                plan["dry_run"] = True
                plan["idempotent"] = False
                return plan

            cycle_id = self._insert_cycle(conn, project_id, plan, idempotency_key, fingerprint)
        except Exception as exc:
            return self._error(ERROR_INTERNAL_ERROR, str(exc))
        finally:
            conn.close()

        action_result = self._execute_plan(project_id, plan, idempotency_key)
        self._update_cycle_result(cycle_id, action_result)
        return {
            "success": bool(action_result.get("success", False)),
            "cycle_id": cycle_id,
            "project_id": project_id,
            "task_id": plan.get("task_id"),
            "planned_action": plan.get("planned_action"),
            "selected_actor_id": plan.get("selected_actor_id", ""),
            "action_result": self._sanitize_result(action_result),
            "idempotent": False,
            "error_code": action_result.get("error_code"),
        }

    def _plan(self, conn, project_id: int) -> Dict[str, Any]:
        task = self._select_candidate_task(conn, project_id)
        if task is None:
            return self._plan_result(project_id, None, "NO_ACTION", "No V2 task requires action")
        state = str(task["status"]).lower()
        if state == "need_user":
            return self._plan_result(project_id, task, "STOP_AND_WAIT_USER", "Task requires user decision")
        if state == "blocked":
            return self._plan_result(project_id, task, "STOP_AND_REPORT_BLOCKER", "Task is blocked")
        if state == "result_submitted":
            result = self._latest_result(conn, int(task["id"]))
            reviewer = self._select_actor(conn, "reviewer", task, exclude_worker_id=(result or {}).get("worker_id"))
            if not result:
                return self._plan_result(project_id, task, "WAIT_RESULT_RECORD", "Result packet is missing")
            if not reviewer:
                return self._plan_result(project_id, task, "WAIT_REVIEWER", "No available reviewer")
            return self._plan_result(project_id, task, "BEGIN_REVIEW", "Begin reviewer inspection", reviewer["worker_id"], result_id=result["result_id"])
        if state == "rework":
            worker = self._select_actor(conn, "executor", task)
            if not worker:
                return self._plan_result(project_id, task, "WAIT_EXECUTOR", "No available executor for rework")
            return self._plan_result(project_id, task, "PLAN_REWORK_HANDOFF", "Rework requires a new executor handoff/requeue", worker["worker_id"])
        if state == "queued":
            worker = self._select_actor(conn, "executor", task)
            if not worker:
                return self._plan_result(project_id, task, "WAIT_EXECUTOR", "No available executor")
            return self._plan_result(project_id, task, "CLAIM_TASK", "Claim queued task", worker["worker_id"])
        if state in WAIT_STATES:
            return self._plan_result(project_id, task, "WAIT_WORKER" if state in {"claimed", "running"} else "WAIT_REVIEW_DECISION", "Existing actor is still responsible")
        if state in TERMINAL_STATES:
            return self._plan_result(project_id, task, "NO_ACTION", "Task is terminal")
        return self._plan_result(project_id, task, "NO_ACTION", f"State {state} has no deterministic action")

    def _execute_plan(self, project_id: int, plan: Dict[str, Any], idempotency_key: str) -> Dict[str, Any]:
        action = plan.get("planned_action")
        if action == "CLAIM_TASK":
            return TaskClaimService(self.db_path, v2_enabled=True).claim_task(
                task_id=int(plan["task_id"]),
                worker_id=plan["selected_actor_id"],
                expected_version=int(plan["state_version"]),
                idempotency_key=f"supervisor-claim:{idempotency_key}",
                allowed_task_ids=[int(plan["task_id"])],
                project_id=project_id,
            )
        if action == "BEGIN_REVIEW":
            return TaskReviewService(self.db_path, v2_enabled=True).begin_review(
                task_id=int(plan["task_id"]),
                result_id=plan["result_id"],
                reviewer_id=plan["selected_actor_id"],
                expected_version=int(plan["state_version"]),
                idempotency_key=f"supervisor-review:{idempotency_key}",
            )
        return {"success": True, "action": action, "message": plan.get("reason", ""), "error_code": None}

    def _select_candidate_task(self, conn, project_id: int):
        priority = {
            "need_user": 1,
            "blocked": 2,
            "result_submitted": 3,
            "reviewing": 4,
            "rework": 5,
            "queued": 6,
            "claimed": 7,
            "running": 8,
            "verified": 9,
            "cancelled": 10,
            "failed": 11,
        }
        rows = conn.execute(
            "SELECT * FROM development_tasks WHERE project_id=? ORDER BY id",
            (project_id,),
        ).fetchall()
        if not rows:
            return None
        return sorted(rows, key=lambda r: (priority.get(str(r["status"]).lower(), 99), int(r["id"])))[0]

    def _select_actor(self, conn, worker_type: str, task, exclude_worker_id: Optional[str] = None):
        rows = conn.execute("""
            SELECT worker_id, worker_type, status, current_load, max_concurrency, last_seen_at
            FROM agent_workers
            WHERE worker_type=? AND status='available'
            ORDER BY worker_id
        """, (worker_type,)).fetchall()
        for row in rows:
            if exclude_worker_id and row["worker_id"] == exclude_worker_id:
                continue
            if int(row["current_load"] or 0) >= int(row["max_concurrency"] or 1):
                continue
            if self._heartbeat_stale(row["last_seen_at"]):
                continue
            if not self._project_scope_allows(conn, row["worker_id"], int(task["project_id"])):
                continue
            if worker_type == "executor" and not self._capabilities_allow(conn, row["worker_id"], task):
                continue
            return row
        return None

    def _latest_result(self, conn, task_id: int):
        row = conn.execute(
            "SELECT result_id, worker_id FROM task_results WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def _capabilities_allow(self, conn, worker_id: str, task) -> bool:
        raw = task["implementation_steps"] if "implementation_steps" in task.keys() else None
        reqs: List[str] = []
        try:
            data = json.loads(raw) if raw else {}
            req = data.get("_requirements", {}) if isinstance(data, dict) else {}
            reqs = [str(v).lower() for v in req.values() if v]
        except Exception:
            reqs = []
        if not reqs:
            return True
        if not self._table_exists(conn, "agent_capabilities"):
            return False
        caps = {r["capability"].lower() for r in conn.execute("SELECT capability FROM agent_capabilities WHERE worker_id=?", (worker_id,)).fetchall()}
        return all(req in caps for req in reqs)

    def _project_scope_allows(self, conn, worker_id: str, project_id: int) -> bool:
        if not self._table_exists(conn, "worker_project_scopes"):
            return True
        row = conn.execute("SELECT 1 FROM worker_project_scopes WHERE worker_id=? AND project_id=?", (worker_id, project_id)).fetchone()
        any_scope = conn.execute("SELECT 1 FROM worker_project_scopes WHERE worker_id=?", (worker_id,)).fetchone()
        return row is not None or any_scope is None

    def _heartbeat_stale(self, value: Any) -> bool:
        if not value:
            return True
        try:
            seen = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return True
        return seen < datetime.now() - timedelta(seconds=self.stale_heartbeat_seconds)

    def _insert_cycle(self, conn, project_id: int, plan: Dict[str, Any], idempotency_key: str, fingerprint: str) -> str:
        cycle_id = f"cyc-{uuid.uuid4().hex[:16]}"
        now = self._now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("""
                INSERT INTO supervisor_cycles
                (cycle_id, project_id, task_id, observed_state, state_version, planned_action,
                 selected_actor_id, dry_run, result, result_json, idempotency_key, request_fingerprint,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'started', '{}', ?, ?, ?, ?)
            """, (
                cycle_id, project_id, plan.get("task_id"), plan.get("observed_state", ""),
                plan.get("state_version"), plan.get("planned_action"), plan.get("selected_actor_id", ""),
                idempotency_key, fingerprint, now, now,
            ))
            conn.commit()
            return cycle_id
        except Exception:
            conn.rollback()
            raise

    def _update_cycle_result(self, cycle_id: str, result: Dict[str, Any]) -> None:
        conn = self._get_conn()
        try:
            safe = self._sanitize_result(result)
            conn.execute(
                "UPDATE supervisor_cycles SET result=?, result_json=?, updated_at=? WHERE cycle_id=?",
                ("success" if result.get("success") else "failed", self._json(safe), self._now(), cycle_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _cycle_by_key(self, conn, idempotency_key: str):
        row = conn.execute("SELECT * FROM supervisor_cycles WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row is None:
            return None
        action_result = self._loads(row["result_json"], {})
        success = row["result"] != "failed"
        return {
            "success": success,
            "cycle_id": row["cycle_id"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "planned_action": row["planned_action"],
            "selected_actor_id": row["selected_actor_id"],
            "action_result": action_result,
            "idempotent": True,
            "error_code": action_result.get("error_code") if isinstance(action_result, dict) else None,
        }

    def _fingerprint(self, project_id: int, plan: Dict[str, Any], dry_run: bool) -> str:
        return self._hash({
            "project_id": project_id,
            "task_id": plan.get("task_id"),
            "state": plan.get("observed_state"),
            "state_version": plan.get("state_version"),
            "selected_actor_id": plan.get("selected_actor_id"),
            "planned_action": plan.get("planned_action"),
            "dry_run": dry_run,
        })

    def _plan_result(self, project_id: int, task, action: str, reason: str, actor_id: str = "", **extra):
        result = {
            "success": True,
            "project_id": project_id,
            "task_id": int(task["id"]) if task is not None else None,
            "observed_state": str(task["status"]).upper() if task is not None else "",
            "state_version": int(task["state_version"]) if task is not None else None,
            "planned_action": action,
            "selected_actor_id": actor_id,
            "reason": reason,
            "error_code": None,
        }
        result.update(extra)
        return result

    def _sanitize_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        safe = dict(result or {})
        for key in list(safe):
            compact = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if "token" in compact or "fingerprint" in compact:
                safe.pop(key, None)
        if isinstance(safe.get("task_packet"), dict):
            safe["task_packet"] = dict(safe["task_packet"])
            safe["task_packet"].pop("lease_token", None)
        return safe

    def _safe_worker(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in row.items() if "token" not in k.lower()}

    def _table_exists(self, conn, name: str) -> bool:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _loads(self, value: Any, default: Any) -> Any:
        try:
            return json.loads(value) if isinstance(value, str) else value
        except Exception:
            return default

    def _hash(self, value: Any) -> str:
        return hashlib.sha256(self._json(value).encode("utf-8")).hexdigest()

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _error(self, code: str, message: str) -> Dict[str, Any]:
        return {"success": False, "error_code": code, "error_message": message, "idempotent": False}
