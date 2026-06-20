"""RecoveryManager - 重启恢复管理器

负责：
- 扫描未结束 run
- 判断 heartbeat 是否过期
- 原子接管过期 run
- 检查 task lease
- 检查 execution 记录
- 检查 Git 状态
- 避免重复 CLI 调用
- completed 任务绝不重跑
- 无法安全恢复时回滚并 blocked
"""
import sqlite3
import json
import uuid
from typing import Optional, List, Dict, Any
from pathlib import Path


class RecoveryManager:
    """重启恢复管理器"""

    def __init__(self, db_path: str, repo_path: str = None):
        self.db_path = db_path
        self.repo_path = repo_path

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def scan_unfinished_runs(self) -> List[Dict[str, Any]]:
        """
        扫描所有未结束的 run（非终态）。
        返回按 id DESC 排序的列表。
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM executor_runs
                WHERE status NOT IN ('completed', 'blocked', 'failed', 'idle')
                ORDER BY id DESC
            """)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def is_heartbeat_expired(self, run: Dict[str, Any],
                             timeout_seconds: int = 120) -> bool:
        """
        判断 heartbeat 是否过期。
        如果 heartbeat_at 为 None，视为过期。
        """
        heartbeat_at = run.get("heartbeat_at")
        if not heartbeat_at:
            return True

        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT datetime('now','localtime',?) > ? as expired
            """, (f'-{timeout_seconds} seconds', heartbeat_at))
            row = cur.fetchone()
            return bool(row["expired"]) if row else True
        finally:
            conn.close()

    def get_recovery_state(self, run: Dict[str, Any]) -> Dict[str, Any]:
        """
        获取 run 的恢复状态信息：
        - 当前任务状态
        - lease 状态
        - execution 记录
        - 是否需要回滚
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()

            result = {
                "run_id": run["run_id"],
                "status": run["status"],
                "current_task_id": run.get("current_task_id"),
                "worker_id": run.get("worker_id"),
                "has_active_lease": False,
                "last_execution": None,
                "task_status": None,
                "needs_rollback": False,
                "can_safely_resume": True,
                "issues": [],
            }

            task_id = run.get("current_task_id")
            if task_id:
                # 检查任务状态
                cur.execute(
                    "SELECT id, status FROM development_tasks WHERE id = ?",
                    (task_id,)
                )
                task_row = cur.fetchone()
                if task_row:
                    result["task_status"] = task_row["status"]

                    # completed 任务绝不重跑
                    if task_row["status"] == "completed":
                        result["can_safely_resume"] = False
                        result["issues"].append("current_task already completed")

                    # blocked 任务不重跑
                    if task_row["status"] == "blocked":
                        result["can_safely_resume"] = False
                        result["issues"].append("current_task is blocked")

                # 检查 lease
                cur.execute("""
                    SELECT id, status FROM task_leases
                    WHERE task_id = ? AND status = 'active'
                """, (task_id,))
                lease_row = cur.fetchone()
                if lease_row:
                    result["has_active_lease"] = True

                # 检查最近 execution
                cur.execute("""
                    SELECT id, status, repair_count, test_result
                    FROM executions
                    WHERE task_id = ?
                    ORDER BY id DESC LIMIT 1
                """, (task_id,))
                exec_row = cur.fetchone()
                if exec_row:
                    result["last_execution"] = dict(exec_row)

            # 检查是否需要 Git 回滚
            if run["status"] in ("executing", "testing", "repairing"):
                result["needs_rollback"] = True

            return result
        finally:
            conn.close()

    def attempt_recovery(self, run: Dict[str, Any],
                         heartbeat_timeout: int = 120) -> Dict[str, Any]:
        """
        尝试恢复一个心跳过期的 run。

        步骤：
        1. 检查 heartbeat 是否过期
        2. 获取恢复状态
        3. 如果无法安全恢复，标记为 blocked
        4. 如果可恢复，原子接管并更新状态为 starting

        Returns:
            {"action": "blocked"|"resumed"|"skipped", "reason": str}
        """
        run_id = run["run_id"]
        project_id = run.get("project_id")
        status = run.get("status", "")

        # 1. 检查心跳
        if not self.is_heartbeat_expired(run, heartbeat_timeout):
            return {"action": "skipped", "reason": "heartbeat not expired"}

        # 2. 获取恢复状态
        recovery_state = self.get_recovery_state(run)

        # 3. 判断是否可以安全恢复
        if not recovery_state["can_safely_resume"]:
            # 无法安全恢复，标记为 blocked
            conn = self._get_conn()
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE executor_runs
                    SET status = 'blocked',
                        pause_reason = ?,
                        finished_at = datetime('now','localtime'),
                        last_error = ?
                    WHERE run_id = ?
                """, (
                    f"recovery_blocked: {', '.join(recovery_state['issues'])}",
                    f"recovery_blocked: {', '.join(recovery_state['issues'])}",
                    run_id,
                ))
                conn.commit()
                return {"action": "blocked",
                        "reason": f"recovery_blocked: {recovery_state['issues']}"}
            finally:
                conn.close()

        # 4. 释放过期的 task lease
        task_id = run.get("current_task_id")
        if task_id and recovery_state.get("has_active_lease"):
            self._release_expired_lease(task_id)

        # 5. 接管并恢复（重新扫描队列）
        new_worker_id = f"runner-{uuid.uuid4().hex[:12]}"
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # 原子接管：new_worker_id ≠ run_id，确保参数顺序正确
            cur.execute("""
                UPDATE executor_runs
                SET status = 'starting',
                    worker_id = ?,
                    heartbeat_at = datetime('now','localtime'),
                    current_task_id = NULL,
                    current_step = 'recovery'
                WHERE run_id = ?
                AND status IN ('starting','scanning','claiming','executing',
                               'testing','repairing','paused','stopping')
                AND (
                    heartbeat_at IS NULL
                    OR heartbeat_at < datetime('now','localtime',?)
                )
            """, (new_worker_id, run_id, f'-{heartbeat_timeout} seconds'))

            if cur.rowcount == 0:
                conn.rollback()
                return {"action": "skipped",
                        "reason": "race condition - another process took over"}

            conn.commit()
            return {"action": "resumed",
                    "reason": f"recovered from {status}", "run_id": run_id}
        except Exception as e:
            conn.rollback()
            return {"action": "failed", "reason": str(e)}
        finally:
            conn.close()

    def _release_expired_lease(self, task_id: int):
        """释放过期 lease"""
        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE task_leases
                SET status='expired', released_at=datetime('now')
                WHERE task_id=? AND status='active'
            """, (task_id,))
            conn.commit()
        finally:
            conn.close()

    def cleanup_orphan_leases(self, project_id: int = None):
        """
        清理孤儿 lease（任务状态非 pending/executing 但仍有活跃 lease）。
        """
        conn = self._get_conn()
        try:
            query = """
                UPDATE task_leases
                SET status='expired', released_at=datetime('now')
                WHERE status='active'
                AND task_id IN (
                    SELECT id FROM development_tasks
                    WHERE status NOT IN ('pending', 'executing', 'waiting_test')
                )
            """
            if project_id is not None:
                query += " AND task_id IN (SELECT id FROM development_tasks WHERE project_id = ?)"
                conn.execute(query, (project_id,))
            else:
                conn.execute(query)
            conn.commit()
        finally:
            conn.close()

    def check_git_state(self) -> Dict[str, Any]:
        """
        检查 Git 工作区状态（仅当 repo_path 设置时）。
        """
        if not self.repo_path:
            return {"ok": True, "reason": "no repo_path configured"}

        import subprocess
        repo = Path(self.repo_path)
        if not repo.exists():
            return {"ok": False, "reason": "repo path does not exist"}

        try:
            # 检查是否为 Git 仓库
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=str(repo), capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return {"ok": False, "reason": "not a git repository"}

            # 检查 detached HEAD
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo), capture_output=True, text=True, timeout=10
            )
            is_detached = result.stdout.strip() == "HEAD"

            # 检查工作区状态
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(repo), capture_output=True, text=True, timeout=10
            )
            is_clean = len(result.stdout.strip()) == 0

            return {
                "ok": True,
                "is_detached": is_detached,
                "is_clean": is_clean,
                "head_ref": result.stdout.strip()[:500] if not is_detached else "DETACHED",
            }
        except Exception as e:
            return {"ok": False, "reason": str(e)}
