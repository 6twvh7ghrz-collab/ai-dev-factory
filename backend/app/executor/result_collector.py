"""结果收集器 - 收集执行结果并持久化到数据库"""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime
from pathlib import Path


@dataclass
class ExecutionRecord:
    """执行记录"""
    id: int = 0
    task_id: int = 0
    project_id: int = 0
    worker_id: str = "worker-default"
    status: str = "pending"
    worktree_path: str = ""
    worktree_branch: str = ""
    start_commit: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    repair_count: int = 0
    max_repairs: int = 2
    exit_code: Optional[int] = None
    test_result: str = "not_run"
    execution_result: str = ""
    error_message: str = ""
    safety_passed: int = 0
    files_checked: str = "[]"
    files_modified: str = "[]"
    model_calls: int = 0
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ExecutionLog:
    """执行步骤日志 (V1.8: 新增 resolved_executable, error, timed_out, killed, cwd)"""
    id: int = 0
    execution_id: int = 0
    step_name: str = ""
    step_status: str = "running"
    command: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    duration_ms: int = 0
    detail: str = ""
    resolved_executable: str = ""   # V1.8: 实际解析到的可执行文件路径
    error: str = ""                  # V1.8: 异常消息
    timed_out: int = 0               # V1.8: 是否超时 (0/1)
    killed: int = 0                  # V1.8: 是否被终止 (0/1)
    cwd: str = ""                    # V1.8: 执行工作目录
    created_at: str = ""


class ResultCollector:
    """结果收集器 - 数据库读写"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── 任务领取 ──

    def claim_task(self, task_id: int, worker_id: str = "worker-default",
                   lease_seconds: int = 3600) -> bool:
        """原子领取任务（单条 UPSERT SQL）

        同一 task_id 始终最多一条 lease 记录，SQL 本身保证原子性：
        - 无记录 → INSERT 新 lease
        - 有 active 未过期 → 拒绝（UPDATE WHERE 不命中，rowcount=0）
        - 有 active 已过期 → UPDATE 接管
        - 有 expired/released → UPDATE 复用原记录
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
                SELECT ?, ?, 'active', datetime('now','localtime'), datetime('now','localtime', ?)
                WHERE EXISTS (
                    SELECT 1 FROM development_tasks
                    WHERE id = ? AND status = 'pending'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM task_leases
                    WHERE task_id = ?
                    AND status = 'active'
                    AND expires_at > datetime('now','localtime')
                    AND expires_at IS NOT NULL
                )
                ON CONFLICT(task_id) DO UPDATE
                SET worker_id = excluded.worker_id,
                    status = 'active',
                    locked_at = excluded.locked_at,
                    expires_at = excluded.expires_at,
                    released_at = NULL
                WHERE (
                    task_leases.status != 'active'
                    OR task_leases.expires_at <= datetime('now','localtime')
                    OR task_leases.expires_at IS NULL
                )
            """, (task_id, worker_id, f"+{lease_seconds} seconds",
                  task_id, task_id))
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            return False

    def release_lease(self, task_id: int):
        """释放任务租约"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE task_leases SET status='released', released_at=datetime('now') WHERE task_id=? AND status='active'",
            (task_id,)
        )
        conn.commit()

    # ── 执行记录 ──

    def create_execution(self, task_id: int, project_id: int,
                         worker_id: str = "worker-default") -> ExecutionRecord:
        """创建执行记录"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO executions (task_id, project_id, worker_id, status, started_at)
            VALUES (?, ?, ?, 'running', datetime('now'))
        """, (task_id, project_id, worker_id))
        conn.commit()

        exec_id = cur.lastrowid
        return self.get_execution(exec_id)

    def get_execution(self, execution_id: int) -> Optional[ExecutionRecord]:
        """获取执行记录"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM executions WHERE id = ?", (execution_id,))
        row = cur.fetchone()
        if not row:
            return None
        return ExecutionRecord(**dict(row))

    def update_execution(self, execution_id: int, **kwargs):
        """更新执行记录"""
        if not kwargs:
            return
        conn = self._get_conn()
        sets = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values())
        values.append(execution_id)
        conn.execute(
            f"UPDATE executions SET {sets}, updated_at=datetime('now') WHERE id=?",
            values
        )
        conn.commit()

    def update_task_status(self, task_id: int, status: str,
                           execution_result: str = None):
        """更新任务状态"""
        conn = self._get_conn()
        if execution_result:
            conn.execute(
                "UPDATE development_tasks SET status=?, execution_result=?, updated_at=datetime('now') WHERE id=?",
                (status, execution_result, task_id)
            )
        else:
            conn.execute(
                "UPDATE development_tasks SET status=?, updated_at=datetime('now') WHERE id=?",
                (status, task_id)
            )
        conn.commit()

    # ── 执行日志 ──

    def add_log(self, execution_id: int, step_name: str, step_status: str = "running",
                command: str = "", stdout: str = "", stderr: str = "",
                exit_code: int = None, duration_ms: int = 0, detail: str = "",
                resolved_executable: str = "", error: str = "",
                timed_out: int = 0, killed: int = 0, cwd: str = ""):
        """添加执行日志 (V1.8: 新增 resolved_executable, error, timed_out, killed, cwd)"""
        self._ensure_v18_schema()  # V1.8: 自动迁移
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO execution_logs
            (execution_id, step_name, step_status, command, stdout, stderr,
             exit_code, duration_ms, detail,
             resolved_executable, error, timed_out, killed, cwd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (execution_id, step_name, step_status, command, stdout, stderr,
              exit_code, duration_ms, detail,
              resolved_executable, error, timed_out, killed, cwd))
        conn.commit()

    # ── V1.8 Schema 迁移 ──
    _v18_migrated: bool = False

    def _ensure_v18_schema(self):
        """V1.8: 确保 execution_logs 表包含所有必要字段"""
        if ResultCollector._v18_migrated:
            return
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(execution_logs)")
        existing_cols = {row[1] for row in cur.fetchall()}
        v18_cols = {
            "resolved_executable": "TEXT DEFAULT ''",
            "error": "TEXT DEFAULT ''",
            "timed_out": "INTEGER DEFAULT 0",
            "killed": "INTEGER DEFAULT 0",
            "cwd": "TEXT DEFAULT ''",
        }
        for col_name, col_def in v18_cols.items():
            if col_name not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE execution_logs ADD COLUMN {col_name} {col_def}")
                except Exception:
                    pass  # 列已存在或数据库锁定
        conn.commit()
        ResultCollector._v18_migrated = True

    def get_logs(self, execution_id: int) -> List[ExecutionLog]:
        """获取执行日志"""
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM execution_logs WHERE execution_id = ? ORDER BY id",
            (execution_id,)
        )
        return [ExecutionLog(**dict(row)) for row in cur.fetchall()]

    def get_executions(self, task_id: int = None, status: str = None,
                       limit: int = 50) -> List[ExecutionRecord]:
        """获取执行记录列表"""
        conn = self._get_conn()
        cur = conn.cursor()
        query = "SELECT * FROM executions WHERE 1=1"
        params = []
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur.execute(query, params)
        return [ExecutionRecord(**dict(row)) for row in cur.fetchall()]

    def get_running_executions(self) -> List[ExecutionRecord]:
        """获取正在运行的执行记录"""
        return self.get_executions(status="running")

    # ── Bug 关联 ──

    def create_bug(self, project_id: int, title: str, task_id: int = None,
                   execution_id: int = None, description: str = "",
                   error_message: str = "", files_changed: str = "",
                   test_result: str = "") -> int:
        """创建 Bug 记录（指纹去重：同 task+execution+fingerprint 只创建一条）"""
        import hashlib
        conn = self._get_conn()
        cur = conn.cursor()

        # 生成失败指纹
        finger_raw = f"{task_id}|{execution_id}|{error_message[:200]}|{test_result}"
        failure_fingerprint = hashlib.sha256(finger_raw.encode()).hexdigest()[:32]

        # 指纹去重检查
        cur.execute("""
            SELECT id FROM bugs
            WHERE task_id = ? AND execution_id = ? AND failure_fingerprint = ?
            LIMIT 1
        """, (task_id, execution_id, failure_fingerprint))
        existing = cur.fetchone()
        if existing:
            return existing[0]  # 返回已有 Bug ID，不重复创建

        cur.execute("""
            INSERT INTO bugs (project_id, task_id, execution_id, title, description,
                             error_message, files_changed, test_result, status, repair_attempt,
                             failure_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reported', 0, ?)
        """, (project_id, task_id, execution_id, title, description,
              error_message, files_changed, test_result, failure_fingerprint))
        conn.commit()
        return cur.lastrowid

    def update_bug_status(self, bug_id: int, status: str, reason: str = "",
                          operator: str = "executor"):
        """更新 Bug 状态（自动记录日志，允许完整的合法状态流转）"""
        conn = self._get_conn()
        cur = conn.cursor()

        # 获取当前状态
        cur.execute("SELECT status FROM bugs WHERE id = ?", (bug_id,))
        row = cur.fetchone()
        if not row:
            return
        from_status = row["status"]

        # 更新状态
        cur.execute(
            "UPDATE bugs SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, bug_id)
        )

        # 写入状态日志
        cur.execute("""
            INSERT INTO bug_status_logs (bug_id, from_status, to_status, reason, operator)
            VALUES (?, ?, ?, ?, ?)
        """, (bug_id, from_status, status, reason, operator))

        conn.commit()
