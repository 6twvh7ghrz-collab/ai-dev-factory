"""
Section 二：RecoveryManager worker_id 修复验证

验证 attempt_recovery() 接管的 worker_id：
- 不等于 run_id
- 以 runner- 开头
- 活跃心跳不能接管
- 过期心跳只能一个 Worker 接管
- 终态 run 不能接管
"""
import sqlite3
import uuid
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.executor.recovery_manager import RecoveryManager


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def create_recovery_test_db(db_path: str):
    conn = _connect(db_path)
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT DEFAULT '', status TEXT DEFAULT 'active')""")
    cur.execute("INSERT INTO projects (id, name) VALUES (1, 'recovery-test')")

    cur.execute("""CREATE TABLE IF NOT EXISTS development_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
        title TEXT DEFAULT '', status TEXT DEFAULT 'pending',
        updated_at TEXT DEFAULT (datetime('now')))""")
    cur.execute("INSERT INTO development_tasks (id, project_id, title, status) VALUES (1, 1, 'Task-1', 'pending')")

    cur.execute("""CREATE TABLE IF NOT EXISTS executor_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE NOT NULL,
        project_id INTEGER NOT NULL, worker_id TEXT,
        status TEXT DEFAULT 'starting', current_task_id INTEGER,
        heartbeat_at TEXT, current_step TEXT,
        tasks_completed INTEGER DEFAULT 0, tasks_failed INTEGER DEFAULT 0,
        tasks_blocked INTEGER DEFAULT 0,
        started_at TEXT, finished_at TEXT, pause_reason TEXT,
        last_error TEXT, created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')))""")

    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_executor_runs_active_project
        ON executor_runs(project_id)
        WHERE status IN ('starting','scanning','claiming','executing',
                         'testing','repairing','paused','stopping')""")

    cur.execute("""CREATE TABLE IF NOT EXISTS executions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
        project_id INTEGER NOT NULL, worker_id TEXT DEFAULT 'worker-default',
        status TEXT DEFAULT 'pending', repair_count INTEGER DEFAULT 0,
        test_result TEXT DEFAULT 'not_run', started_at TEXT,
        completed_at TEXT, duration_ms INTEGER DEFAULT 0)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS task_leases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL UNIQUE,
        worker_id TEXT, status TEXT DEFAULT 'active',
        locked_at TEXT, expires_at TEXT, released_at TEXT)""")

    conn.commit()
    conn.close()


def seed_run(db_path: str, run_id: str, project_id: int, worker_id: str,
             status: str, heartbeat_offset: int = -999):
    """创建测试 executor_run。heartbeat_offset: 负数为过去"""
    conn = _connect(db_path)
    conn.execute("""
        INSERT INTO executor_runs (run_id, project_id, worker_id, status, current_task_id, heartbeat_at)
        VALUES (?, ?, ?, ?, 1, datetime('now','localtime', ?))
    """, (run_id, project_id, worker_id, status, f"{heartbeat_offset:+d} seconds"))
    conn.commit()
    conn.close()


class TestRecoveryWorkerId:
    """验证 RecoveryManager worker_id 修复"""

    def test_worker_id_not_equals_run_id(self):
        """接管后 worker_id 不等于 run_id"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_recovery_test_db(db_path)
            run_id = f"run-{uuid.uuid4().hex[:8]}"
            seed_run(db_path, run_id, 1, "old-worker", "executing", -999)

            rm = RecoveryManager(db_path)
            result = rm.attempt_recovery(
                {"run_id": run_id, "project_id": 1, "status": "executing", "current_task_id": 1},
                heartbeat_timeout=60
            )

            assert result["action"] == "resumed"

            conn = _connect(db_path)
            row = conn.execute(
                "SELECT worker_id FROM executor_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            conn.close()

            assert row["worker_id"] != run_id, f"worker_id should not equal run_id, got {row['worker_id']}"
            assert row["worker_id"].startswith("runner-"), f"worker_id should start with runner-, got {row['worker_id']}"
        finally:
            os.unlink(db_path)

    def test_active_heartbeat_not_taken_over(self):
        """活跃心跳不能接管"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_recovery_test_db(db_path)
            run_id = f"run-{uuid.uuid4().hex[:8]}"
            # 1秒前的心跳，timeout=60 → 未过期
            seed_run(db_path, run_id, 1, "active-worker", "executing", -1)

            # 从 DB 读取完整 run 数据（含 heartbeat_at）
            conn = _connect(db_path)
            db_run = dict(conn.execute(
                "SELECT * FROM executor_runs WHERE run_id=?", (run_id,)
            ).fetchone())
            conn.close()

            rm = RecoveryManager(db_path)
            result = rm.attempt_recovery(db_run, heartbeat_timeout=60)

            assert result["action"] == "skipped", f"Expected skipped, got {result}"
            assert "not expired" in result["reason"] or "race condition" in result["reason"]
        finally:
            os.unlink(db_path)

    def test_expired_heartbeat_only_one_takeover(self):
        """过期心跳只能一个 Worker 接管（rowcount=1）"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_recovery_test_db(db_path)
            run_id = f"run-{uuid.uuid4().hex[:8]}"
            seed_run(db_path, run_id, 1, "dead-worker", "executing", -999)

            rm = RecoveryManager(db_path)
            result = rm.attempt_recovery(
                {"run_id": run_id, "project_id": 1, "status": "executing", "current_task_id": 1},
                heartbeat_timeout=60
            )

            assert result["action"] == "resumed"

            # 第二次接管应该被跳过（因为心跳已经更新）
            result2 = rm.attempt_recovery(
                {"run_id": run_id, "project_id": 1, "status": "starting", "current_task_id": None},
                heartbeat_timeout=60
            )
            assert result2["action"] == "skipped"
        finally:
            os.unlink(db_path)

    def test_terminal_run_not_taken_over(self):
        """终态 run 不能接管"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_recovery_test_db(db_path)
            run_id = f"run-{uuid.uuid4().hex[:8]}"

            # completed 状态 + 过期心跳
            conn = _connect(db_path)
            conn.execute("""
                INSERT INTO executor_runs (run_id, project_id, worker_id, status, heartbeat_at)
                VALUES (?, 1, 'old-worker', 'completed', datetime('now','localtime','-999 seconds'))
            """, (run_id,))
            conn.commit()
            conn.close()

            rm = RecoveryManager(db_path)
            # scan_unfinished_runs 不返回 completed 状态
            runs = rm.scan_unfinished_runs()
            assert not any(r["run_id"] == run_id for r in runs)

            # attempt_recovery 直接传入 completed run
            result = rm.attempt_recovery(
                {"run_id": run_id, "project_id": 1, "status": "completed"},
                heartbeat_timeout=60
            )
            # should not succeed takeover
            assert result["action"] in ("skipped", "blocked")
        finally:
            os.unlink(db_path)

    def test_old_worker_cannot_renew_after_takeover(self):
        """旧 Worker 接管后不能续心跳"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        try:
            create_recovery_test_db(db_path)
            run_id = f"run-{uuid.uuid4().hex[:8]}"
            old_worker = "old-worker-A"
            seed_run(db_path, run_id, 1, old_worker, "executing", -999)

            rm = RecoveryManager(db_path)
            result = rm.attempt_recovery(
                {"run_id": run_id, "project_id": 1, "status": "executing", "current_task_id": 1},
                heartbeat_timeout=60
            )
            assert result["action"] == "resumed"

            conn = _connect(db_path)
            new_row = conn.execute(
                "SELECT worker_id FROM executor_runs WHERE run_id=?", (run_id,)
            ).fetchone()

            # 验证 worker_id 已变更
            assert new_row["worker_id"] != old_worker
            conn.close()
        finally:
            os.unlink(db_path)
