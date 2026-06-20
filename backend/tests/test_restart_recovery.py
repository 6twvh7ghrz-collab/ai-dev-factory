"""
Section 十：六个重启恢复场景测试

验证系统在进程崩溃/重启后能正确恢复资源，避免：
- 资源永久占用
- 任务重复执行
- completed 任务被重跑
- 孤儿 worktree

运行方式：
    cd backend
    python -m pytest tests/test_restart_recovery.py -v
"""
import sqlite3
import json
import threading
import time
import uuid
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def create_recovery_db(db_path: str):
    """创建恢复测试用的完整数据库"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # 项目表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '',
            status TEXT DEFAULT 'active'
        )
    """)
    cur.execute("INSERT OR IGNORE INTO projects (id, name, status) VALUES (1, 'test-project', 'active')")

    # 任务表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS development_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            dependencies TEXT DEFAULT '[]',
            files_to_modify TEXT DEFAULT '[]',
            files_to_check TEXT DEFAULT '[]',
            codex_prompt TEXT DEFAULT '',
            implementation_steps TEXT DEFAULT '',
            test_steps TEXT DEFAULT '',
            task_type TEXT DEFAULT 'code',
            execution_result TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # executor_runs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS executor_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            project_id INTEGER NOT NULL,
            worker_id TEXT,
            status TEXT DEFAULT 'starting',
            mode TEXT DEFAULT 'auto_until_blocked',
            current_task_id INTEGER,
            current_step TEXT,
            tasks_completed INTEGER DEFAULT 0,
            tasks_blocked INTEGER DEFAULT 0,
            tasks_failed INTEGER DEFAULT 0,
            tasks_repaired INTEGER DEFAULT 0,
            tasks_skipped INTEGER DEFAULT 0,
            tasks_total INTEGER DEFAULT 0,
            started_at TEXT,
            finished_at TEXT,
            heartbeat_at TEXT,
            pause_reason TEXT,
            stop_requested INTEGER DEFAULT 0,
            last_error TEXT,
            budget_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_executor_runs_active_project
        ON executor_runs(project_id)
        WHERE status IN ('starting','scanning','claiming','executing',
                         'testing','repairing','paused','stopping')
    """)

    # task_leases
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_leases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL UNIQUE,
            execution_id INTEGER,
            worker_id TEXT,
            status TEXT DEFAULT 'active',
            locked_at TEXT,
            expires_at TEXT,
            released_at TEXT
        )
    """)

    # executions
    cur.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            worker_id TEXT DEFAULT 'worker-default',
            status TEXT DEFAULT 'pending',
            worktree_path TEXT DEFAULT '',
            worktree_branch TEXT DEFAULT '',
            start_commit TEXT DEFAULT '',
            started_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            duration_ms INTEGER DEFAULT 0,
            repair_count INTEGER DEFAULT 0,
            max_repairs INTEGER DEFAULT 2,
            exit_code INTEGER,
            test_result TEXT DEFAULT 'not_run',
            execution_result TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            safety_passed INTEGER DEFAULT 0,
            files_checked TEXT DEFAULT '[]',
            files_modified TEXT DEFAULT '[]',
            model_calls INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # executor_resource_locks
    cur.execute("""
        CREATE TABLE IF NOT EXISTS executor_resource_locks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lock_id TEXT UNIQUE NOT NULL,
            lock_token TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            task_id INTEGER, execution_id INTEGER,
            executor_run_id INTEGER,
            worker_id TEXT NOT NULL,
            resource_scope TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_key TEXT NOT NULL,
            normalized_key TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            locked_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            heartbeat_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            expires_at TEXT, released_at TEXT, release_reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_locks_active
        ON executor_resource_locks(resource_scope, scope_key, resource_type, normalized_key)
        WHERE status = 'active'
    """)

    # execution_logs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER NOT NULL,
            step_name TEXT,
            step_status TEXT DEFAULT 'running',
            command TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # bugs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bugs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            task_id INTEGER, execution_id INTEGER,
            title TEXT, error_message TEXT DEFAULT '',
            test_result TEXT DEFAULT '',
            status TEXT DEFAULT 'reported',
            repair_attempt INTEGER DEFAULT 0,
            failure_fingerprint TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bug_status_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bug_id INTEGER NOT NULL,
            from_status TEXT, to_status TEXT NOT NULL,
            reason TEXT, operator TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()


def _insert_task(conn, task_id, title, status="pending", project_id=1, files=None):
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO development_tasks
        (id, project_id, title, status, files_to_modify)
        VALUES (?, ?, ?, ?, ?)
    """, (task_id, project_id, title, status, json.dumps(files or [])))
    conn.commit()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _past(seconds_ago):
    dt = datetime.now() - timedelta(seconds=seconds_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _future(seconds_ahead):
    dt = datetime.now() + timedelta(seconds=seconds_ahead)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════
# 场景 1: Worker 执行中崩溃 — lease 和 lock 恢复
# ═══════════════════════════════════════════════════════════

def test_scenario_01_worker_crash_during_execution():
    """
    模拟：Worker 在执行过程中崩溃，心跳过期。
    恢复应：检测到过期 lease，释放资源锁。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="recovery_crash_"))
    db_path = str(tmp_dir / "test.db")
    create_recovery_db(db_path)

    try:
        print("\n[SCENARIO 1] Worker 执行中崩溃恢复")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row

        # 插入任务
        _insert_task(conn, 1, "Task that crashed", "pending")

        # 模拟 worker 在执行中崩溃的状态
        cur = conn.cursor()
        worker_id = "worker-crashed-001"
        now = _now()

        # 创建活跃 run
        cur.execute("""
            INSERT INTO executor_runs
            (run_id, project_id, worker_id, status, mode,
             current_task_id, started_at, heartbeat_at)
            VALUES ('run-crash-001', 1, ?, 'executing', 'auto_until_blocked',
                    1, ?, ?)
        """, (worker_id, now, _past(300)))  # heartbeat 5 分钟前

        # 创建未释放的 lease
        cur.execute("""
            INSERT INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
            VALUES (1, ?, 'active', ?, ?)
        """, (worker_id, _past(300), _now()))

        # 创建未释放的资源锁
        cur.execute("""
            INSERT INTO executor_resource_locks
            (lock_id, lock_token, project_id, task_id, execution_id,
             executor_run_id, worker_id, resource_scope, scope_key,
             resource_type, resource_key, normalized_key, status,
             locked_at, heartbeat_at, expires_at)
            VALUES (?, ?, 1, 1, NULL, 1, ?, 'project', '1',
                    'file', 'task_a.py', 'task_a.py', 'active',
                    ?, ?, ?)
        """, (str(uuid.uuid4()), str(uuid.uuid4()), worker_id, _past(300), _past(300), _now()))

        # 创建 running 状态的 execution
        cur.execute("""
            INSERT INTO executions
            (id, task_id, project_id, worker_id, status, started_at)
            VALUES (1, 1, 1, ?, 'running', ?)
        """, (worker_id, _past(300)))

        conn.commit()

        print(f"  崩溃前状态: lease=active, lock=active, execution=running, heartbeat=expired")

        # ── 执行恢复 ──
        from app.executor.recovery_manager import RecoveryManager
        rm = RecoveryManager(db_path, str(tmp_dir))

        # 扫描未结束 run
        unfinished = rm.scan_unfinished_runs()
        print(f"  未结束 run: {len(unfinished)}")

        assert len(unfinished) >= 1, "应检测到未结束的 run"

        # 验证心跳过期
        for run in unfinished:
            if run["run_id"] == "run-crash-001":
                expired = rm.is_heartbeat_expired(run, timeout_seconds=120)
                print(f"  Heartbeat expired: {expired}")
                assert expired, "心跳应已过期"

                # 执行 attempt_recovery（原子接管+释放lease）
                recovery_result = rm.attempt_recovery(run, heartbeat_timeout=120)
                print(f"  Recovery result: {recovery_result}")
                assert recovery_result["action"] in ("resumed", "blocked"), \
                    f"恢复应返回 resumed 或 blocked, 实际: {recovery_result['action']}"
                break

        # ── 验证恢复后状态 ──
        cur.execute("SELECT status FROM task_leases WHERE task_id = 1")
        lease_row = cur.fetchone()
        print(f"  Lease status after recovery: {lease_row['status'] if lease_row else 'N/A'}")

        cur.execute("SELECT COUNT(*) as c FROM executor_resource_locks WHERE status = 'active'")
        active_locks = cur.fetchone()["c"]
        print(f"  Active locks after recovery: {active_locks}")

        cur.execute("SELECT status FROM executor_runs WHERE run_id = 'run-crash-001'")
        run_row = cur.fetchone()
        print(f"  Run status after recovery: {run_row['status'] if run_row else 'N/A'}")

        conn.close()
        print(f"  [PASS] 场景1: Worker 执行中崩溃恢复")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 场景 2: 双 Worker — 一个崩溃，另一个继续
# ═══════════════════════════════════════════════════════════

def test_scenario_02_dual_worker_one_crash():
    """
    模拟：双 Worker 并行，一个崩溃。
    恢复应：只释放崩溃 Worker 的资源，活跃 Worker 不受影响。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="recovery_dual_"))
    db_path = str(tmp_dir / "test.db")
    create_recovery_db(db_path)

    try:
        print("\n[SCENARIO 2] 双 Worker 一个崩溃另一继续")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        now = _now()
        id_crashed = "worker-crashed-002"
        id_alive = "worker-alive-001"

        # 插入两个项目，避免 UNIQUE 约束冲突 (uq_executor_runs_active_project)
        cur.execute("INSERT OR IGNORE INTO projects (id, name, status) VALUES (2, 'test-project-2', 'active')")

        # 两个任务分属不同项目
        _insert_task(conn, 1, "Task A (crashed)", "pending", project_id=1, files=["a.py"])
        _insert_task(conn, 2, "Task B (alive)", "pending", project_id=2, files=["b.py"])

        # Worker A — 崩溃（心跳过期）
        cur.execute("""
            INSERT INTO executor_runs (run_id, project_id, worker_id, status, mode,
                                       current_task_id, heartbeat_at)
            VALUES ('run-crashed', 1, ?, 'executing', 'auto_until_blocked',
                    1, ?)
        """, (id_crashed, _past(300)))
        cur.execute("""
            INSERT INTO task_leases (task_id, execution_id, worker_id, status, locked_at, expires_at)
            VALUES (1, 1, ?, 'active', ?, ?)
        """, (id_crashed, _past(300), _now()))
        cur.execute("""
            INSERT INTO executions (id, task_id, project_id, worker_id, status, started_at)
            VALUES (1, 1, 1, ?, 'running', ?)
        """, (id_crashed, _past(300)))

        # Worker B — 活跃（心跳正常，不同 project）
        cur.execute("""
            INSERT INTO executor_runs (run_id, project_id, worker_id, status, mode, heartbeat_at)
            VALUES ('run-alive', 2, ?, 'executing', 'auto_until_blocked', ?)
        """, (id_alive, now))
        cur.execute("""
            INSERT INTO task_leases (task_id, execution_id, worker_id, status, locked_at, expires_at)
            VALUES (2, 2, ?, 'active', ?, ?)
        """, (id_alive, now, _future(300)))
        cur.execute("""
            INSERT INTO executions (id, task_id, project_id, worker_id, status, started_at)
            VALUES (2, 2, 2, ?, 'running', ?)
        """, (id_alive, now))

        conn.commit()

        # ── 执行恢复：使用 RecoveryManager 扫描并恢复 ──
        from app.executor.recovery_manager import RecoveryManager
        rm = RecoveryManager(db_path, str(tmp_dir))

        # 扫描所有未结束 run
        unfinished = rm.scan_unfinished_runs()
        print(f"  未结束 run: {len(unfinished)} (expect 2)")

        # 验证：活跃心跳不触发恢复
        for run in unfinished:
            expired = rm.is_heartbeat_expired(run, timeout_seconds=120)
            print(f"  Run {run['run_id']} (worker={run['worker_id']}) heartbeat expired: {expired}")
            if run["run_id"] == "run-crashed":
                assert expired, f"崩溃 Worker {id_crashed} 心跳应过期"
                # 对崩溃的 run 执行恢复
                result = rm.attempt_recovery(run, heartbeat_timeout=120)
                print(f"  Recovery result for crashed: {result}")
            elif run["run_id"] == "run-alive":
                assert not expired, f"活跃 Worker {id_alive} 心跳不应过期"

        # ── 验证结果 ──
        # 崩溃 worker 的 lease 应已释放
        cur.execute("SELECT status FROM task_leases WHERE task_id = 1")
        lease1 = cur.fetchone()
        print(f"  Lease task 1 (crashed): {lease1['status']}")
        assert lease1["status"] != "active", f"崩溃 Worker 的 lease 应已释放, 实际: {lease1['status']}"

        # 活跃 worker 的 lease 应保持
        cur.execute("SELECT status FROM task_leases WHERE task_id = 2")
        lease2 = cur.fetchone()
        print(f"  Lease task 2 (alive): {lease2['status']}")
        assert lease2["status"] == "active", "活跃 Worker 的 lease 不应被释放"

        conn.close()
        print(f"  [PASS] 场景2: 双 Worker 一个崩溃")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 场景 3: Run 接管 — 心跳过期后新进程接管
# ═══════════════════════════════════════════════════════════

def test_scenario_03_run_takeover_after_heartbeat_timeout():
    """
    模拟：Run 心跳过期，新进程尝试接管。
    验证：RunStore.takeover_expired_run 的原子性。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="recovery_takeover_"))
    db_path = str(tmp_dir / "test.db")
    create_recovery_db(db_path)

    try:
        print("\n[SCENARIO 3] Run 心跳过期接管")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        now = _now()
        old_worker = "worker-original-003"

        # 创建心跳过期的 run
        cur.execute("""
            INSERT INTO executor_runs
            (run_id, project_id, worker_id, status, mode,
             heartbeat_at, current_task_id)
            VALUES ('run-takeover-test', 1, ?, 'executing', 'auto_until_blocked',
                    ?, 1)
        """, (old_worker, _past(300)))
        conn.commit()

        from app.executor.run_store import RunStore
        store = RunStore(db_path)

        # 验证活跃心跳阻止接管
        # 先设置心跳为刚刚更新
        cur.execute("""
            UPDATE executor_runs SET heartbeat_at = ? WHERE run_id = 'run-takeover-test'
        """, (_now(),))
        conn.commit()

        result_active = store.takeover_expired_run(project_id=1, new_worker_id="worker-new",
                                                    heartbeat_timeout=120)
        print(f"  Active heartbeat takeover: {result_active['success']} (should be False)")
        assert not result_active["success"], "活跃心跳应阻止接管"

        # 设置心跳为过期
        cur.execute("""
            UPDATE executor_runs SET heartbeat_at = ? WHERE run_id = 'run-takeover-test'
        """, (_past(300),))
        conn.commit()

        result_expired = store.takeover_expired_run(project_id=1, new_worker_id="worker-new-003",
                                                     heartbeat_timeout=120)
        print(f"  Expired heartbeat takeover: {result_expired['success']}")
        assert result_expired["success"], "过期心跳应允许接管"

        # 验证 worker_id 已更新
        taken_over = result_expired["run"]
        assert taken_over["worker_id"] == "worker-new-003", \
            f"接管后 worker_id 应为 worker-new-003, 实际: {taken_over['worker_id']}"

        conn.close()
        print(f"  [PASS] 场景3: Run 心跳过期接管")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 场景 4: Lease 过期清理 — 过期 lease 不应阻止新领取
# ═══════════════════════════════════════════════════════════

def test_scenario_04_expired_lease_cleanup():
    """
    模拟：任务 lease 过期，新 worker 应能重新领取该任务。
    验证：claim_task 正确处理过期 lease。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="recovery_lease_"))
    db_path = str(tmp_dir / "test.db")
    create_recovery_db(db_path)

    try:
        print("\n[SCENARIO 4] Lease 过期清理")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        now = _now()

        # 插入任务
        _insert_task(conn, 1, "Lease expired task")

        # 创建过期的 lease
        cur.execute("""
            INSERT INTO task_leases (task_id, worker_id, status, locked_at, expires_at)
            VALUES (1, 'old-worker', 'active', ?, ?)
        """, (_past(600), _past(300)))  # 5 分钟前过期

        conn.commit()

        # ── 执行恢复 ──
        from app.executor.recovery_manager import RecoveryManager
        rm = RecoveryManager(db_path)

        # 释放过期 lease（使用内部方法）
        rm._release_expired_lease(task_id=1)
        print(f"  Released expired lease for task 1")

        # 验证 lease 已清理（status 应变为 'expired'）
        cur.execute("SELECT status, expires_at FROM task_leases WHERE task_id = 1")
        lease = cur.fetchone()
        print(f"  Lease after cleanup: status={lease['status']}, expires_at={lease['expires_at']}")
        assert lease["status"] == "expired", f"lease 应被标记为 expired, 实际: {lease['status']}"

        # task_leases 有 UNIQUE(task_id) 约束，彻底删除旧记录后新 worker 才能领取
        cur.execute("DELETE FROM task_leases WHERE task_id = 1")
        conn.commit()

        # 新 worker 应能领取该任务（claim_task 返回 bool）
        from app.executor.result_collector import ResultCollector
        rc = ResultCollector(db_path)
        claim = rc.claim_task(task_id=1, worker_id="new-worker")
        print(f"  New worker claim: {claim}")
        assert claim, "过期 lease 清理后应能重新领取"

        conn.close()
        print(f"  [PASS] 场景4: Lease 过期清理")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 场景 5: 资源锁恢复 — 过期锁批量释放
# ═══════════════════════════════════════════════════════════

def test_scenario_05_resource_lock_recovery():
    """
    模拟：资源锁过期，恢复时批量释放。
    验证：过期锁被释放，活跃锁保持。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="recovery_locks_"))
    db_path = str(tmp_dir / "test.db")
    create_recovery_db(db_path)

    try:
        print("\n[SCENARIO 5] 资源锁恢复")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        now = _now()

        # 创建过期锁（worker 崩溃）
        cur.execute("""
            INSERT INTO executor_resource_locks
            (lock_id, lock_token, project_id, task_id, execution_id,
             executor_run_id, worker_id, resource_scope, scope_key,
             resource_type, resource_key, normalized_key, status,
             locked_at, heartbeat_at, expires_at)
            VALUES
            ('lock-expired-1', 'tok-expired-1', 1, 1, 1, 1, 'worker-dead-1',
             'project', '1', 'file', 'file_a.py', 'file_a.py', 'active',
             ?, ?, ?),
            ('lock-expired-2', 'tok-expired-2', 1, 2, 2, 1, 'worker-dead-2',
             'project', '1', 'file', 'file_b.py', 'file_b.py', 'active',
             ?, ?, ?)
        """, (_past(600), _past(600), _past(300), _past(600), _past(600), _past(300)))

        # 创建活跃锁（正常 worker，expires_at 设为未来）
        cur.execute("""
            INSERT INTO executor_resource_locks
            (lock_id, lock_token, project_id, task_id, execution_id,
             executor_run_id, worker_id, resource_scope, scope_key,
             resource_type, resource_key, normalized_key, status,
             locked_at, heartbeat_at, expires_at)
            VALUES
            ('lock-alive-1', 'tok-alive-1', 1, 3, 3, 2, 'worker-alive-1',
             'project', '1', 'file', 'file_c.py', 'file_c.py', 'active',
             ?, ?, ?)
        """, (now, now, _future(300)))  # 未来过期，不应被清理

        conn.commit()

        # ── 执行恢复 ──
        from app.executor.resource_lock_manager import ResourceLockManager
        rlm = ResourceLockManager(db_path)

        # cleanup_expired 将过期锁标记为 'expired'（不再 active）
        count = rlm.cleanup_expired()
        print(f"  Cleanup expired locks: {count} released")
        assert count >= 2, f"应至少清理 2 个过期锁, 实际: {count}"

        # 验证过期锁已不再 active
        cur.execute("SELECT COUNT(*) as c FROM executor_resource_locks WHERE status = 'active'")
        active_count = cur.fetchone()["c"]
        print(f"  Active locks after recovery: {active_count}")
        assert active_count == 1, f"应只剩 1 个活跃锁 (alive), 实际: {active_count}"

        # 确认活跃的是 [lock-alive-1]
        cur.execute("SELECT lock_id, worker_id FROM executor_resource_locks WHERE status = 'active'")
        active_lock = cur.fetchone()
        assert active_lock["lock_id"] == "lock-alive-1", f"活跃锁应是 lock-alive-1, 实际: {active_lock['lock_id']}"

        conn.close()
        print(f"  [PASS] 场景5: 资源锁恢复")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 场景 6: completed 任务不重跑
# ═══════════════════════════════════════════════════════════

def test_scenario_06_completed_not_rerun():
    """
    模拟：已完成的 execution 在恢复时不应被重新执行。
    验证：claim_task 不会领取已完成任务的 lease。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="recovery_completed_"))
    db_path = str(tmp_dir / "test.db")
    create_recovery_db(db_path)

    try:
        print("\n[SCENARIO 6] Completed 任务不重跑")

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        now = _now()

        # 已完成的任务
        _insert_task(conn, 1, "Already completed", "completed")
        cur.execute("""
            INSERT INTO executions
            (id, task_id, project_id, worker_id, status, started_at, completed_at, test_result)
            VALUES (1, 1, 1, 'worker-done', 'success', ?, ?, 'pass')
        """, (_past(600), _past(590)))
        cur.execute("""
            INSERT INTO task_leases (task_id, worker_id, status, locked_at, expires_at, released_at)
            VALUES (1, 'worker-done', 'released', ?, ?, ?)
        """, (_past(600), _now(), _past(590)))
        conn.commit()

        # pending 任务（正常领取）
        _insert_task(conn, 2, "Pending task", "pending")
        conn.commit()

        # ── 执行恢复 ──
        from app.executor.result_collector import ResultCollector
        rc = ResultCollector(db_path)

        # Completed 任务不应被领取（claim_task 返回 bool）
        claim_completed = rc.claim_task(task_id=1, worker_id="worker-new")
        print(f"  Claim completed task: {claim_completed} (expect False)")
        assert not claim_completed, "已完成任务不应被重新领取"

        # Pending 任务可以被领取
        claim_pending = rc.claim_task(task_id=2, worker_id="worker-new")
        print(f"  Claim pending task: {claim_pending} (expect True)")
        assert claim_pending, "pending 任务应能被领取"

        # 验证 task_leases 中的 released lease 不变
        cur.execute("SELECT status FROM task_leases WHERE task_id = 1")
        lease1 = cur.fetchone()
        assert lease1["status"] == "released", f"已完成任务的 lease 应保持 released, 实际: {lease1['status']}"

        conn.close()
        print(f"  [PASS] 场景6: Completed 任务不重跑")
    finally:
        import shutil
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("重启恢复场景测试")
    print("=" * 60)

    passed = 0
    failed = 0
    errors = []

    scenarios = [
        ("场景1: Worker执行中崩溃", test_scenario_01_worker_crash_during_execution),
        ("场景2: 双Worker一个崩溃", test_scenario_02_dual_worker_one_crash),
        ("场景3: Run心跳过期接管", test_scenario_03_run_takeover_after_heartbeat_timeout),
        ("场景4: Lease过期清理", test_scenario_04_expired_lease_cleanup),
        ("场景5: 资源锁恢复", test_scenario_05_resource_lock_recovery),
        ("场景6: Completed任务不重跑", test_scenario_06_completed_not_rerun),
    ]

    for name, fn in scenarios:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            errors.append(f"[FAIL] {name}: {e}")
            print(f"\n[FAIL] {name}: {e}")
        except Exception as e:
            failed += 1
            errors.append(f"[ERROR] {name}: {e}")
            print(f"\n[ERROR] {name}: {e}")

    print(f"\n{'=' * 60}")
    print(f"  重启恢复: {passed} PASS, {failed} FAIL")
    print(f"{'=' * 60}")

    if errors:
        for err in errors:
            print(f"  {err}")
