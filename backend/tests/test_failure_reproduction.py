"""
Section 一：6 个失败项固化自动化测试

这些测试覆盖双Worker闭环中已知的资源泄漏、重复执行和恢复问题。
修复前必须稳定失败，修复后必须全部通过。

运行方式：
    cd backend
    python -m pytest tests/test_failure_reproduction.py -v

要求：
    - 测试环境隔离：每个测试使用独立临时 SQLite 数据库
    - 修复前 FAIL：验证已知问题确实存在
    - 修复后 PASS：验证修复措施生效
"""
import sqlite3
import os
import json
import uuid
import threading
import time
from pathlib import Path
import pytest


# ═══════════════════════════════════════════════════════════
# 测试辅助工具
# ═══════════════════════════════════════════════════════════

def create_test_db(db_path: str):
    """创建测试用数据库，包含必要的表结构"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_leases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL UNIQUE,
            worker_id TEXT,
            status TEXT DEFAULT 'active',
            locked_at TEXT,
            expires_at TEXT,
            released_at TEXT
        )
    """)

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER NOT NULL,
            step_name TEXT,
            step_status TEXT DEFAULT 'running',
            command TEXT DEFAULT '',
            stdout TEXT DEFAULT '',
            stderr TEXT DEFAULT '',
            exit_code INTEGER,
            duration_ms INTEGER DEFAULT 0,
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bugs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            task_id INTEGER,
            execution_id INTEGER,
            title TEXT,
            description TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            files_changed TEXT DEFAULT '',
            test_result TEXT DEFAULT '',
            status TEXT DEFAULT 'reported',
            repair_attempt INTEGER DEFAULT 0,
            failure_fingerprint TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bug_status_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bug_id INTEGER NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT,
            operator TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS executor_resource_locks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lock_id TEXT UNIQUE NOT NULL,
            lock_token TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            task_id INTEGER,
            execution_id INTEGER,
            executor_run_id INTEGER,
            worker_id TEXT NOT NULL,
            resource_scope TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_key TEXT NOT NULL,
            normalized_key TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            requires_serial INTEGER DEFAULT 0,
            expires_at TEXT,
            heartbeat_at TEXT,
            released_at TEXT,
            release_reason TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_locks_active
        ON executor_resource_locks(resource_scope, scope_key, resource_type, normalized_key)
        WHERE status = 'active'
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS development_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT DEFAULT '',
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0,
            implementation_steps TEXT DEFAULT '',
            test_steps TEXT DEFAULT '',
            files_to_modify TEXT DEFAULT '[]',
            files_to_check TEXT DEFAULT '[]',
            execution_result TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()


def seed_test_data(db_path: str, project_id: int = 1):
    """插入测试数据：3个pending任务"""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    tasks = [
        (project_id, "Task A: 修复 Calculator 加法", "pending", 1, 1,
         "python fix_calculator.py",
         "pytest test_calculator.py -v",
         '["calculator.py"]'),
        (project_id, "Task B: 修复 Calculator 乘法", "pending", 2, 2,
         "python fix_calculator2.py",
         "pytest test_calculator.py -v -k 'multiply'",
         '["calculator.py"]'),
        (project_id, "Task C: 修复边界条件", "pending", 3, 3,
         "python fix_boundary.py",
         "pytest test_boundary.py -v",
         '["calculator.py", "test_boundary.py"]'),
    ]

    cur.executemany("""
        INSERT INTO development_tasks
        (project_id, title, status, priority, sort_order, implementation_steps, test_steps, files_to_modify)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, tasks)

    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════
# 测试 1: 残余 Worktree 验证
# ═══════════════════════════════════════════════════════════

class TestFailure01_ResidualWorktree:

    def test_worktree_cleanup_on_completion(self, tmp_path):
        """completed 任务应清理 Worktree"""
        db_path = str(tmp_path / "test_worktree.db")
        create_test_db(db_path)
        seed_test_data(db_path)

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        os.system(f'cd "{repo_path}" && git init && git config user.email "test@test.com" && git config user.name "Test" && git commit --allow-empty -m "init"')

        from app.executor.worktree_manager import WorktreeManager
        wtm = WorktreeManager(str(repo_path))

        result = wtm.create_worktree(task_id=1, execution_id=100, base_branch="master")
        assert result["success"], f"创建失败: {result.get('error')}"

        worktrees_before = wtm.list_worktrees()
        assert len(worktrees_before) > 0

        cleanup = wtm.remove_worktree(task_id=1, execution_id=100, force=True)
        assert cleanup["success"], f"清理失败: {cleanup.get('errors')}"

        worktrees_after = wtm.list_worktrees()
        executor_wts = [w for w in worktrees_after if "executor/task-" in w.get("branch", "")]
        assert len(executor_wts) == 0, f"Worktree 未清理: {executor_wts}"

    def test_cleanup_metadata_written(self, tmp_path):
        """清理后应写入 cleanup 元数据"""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        os.system(f'cd "{repo_path}" && git init && git config user.email "test@test.com" && git config user.name "Test" && git commit --allow-empty -m "init"')

        from app.executor.worktree_manager import WorktreeManager
        wtm = WorktreeManager(str(repo_path))

        result = wtm.create_worktree(task_id=2, execution_id=200, base_branch="master")
        assert result["success"]

        cleanup = wtm.remove_worktree(task_id=2, execution_id=200, force=True)
        assert cleanup["success"]

        meta_path = repo_path / ".executor" / "worktree_cleanup_log.json"
        assert meta_path.exists(), f"元数据文件不存在: {meta_path}"

        log_data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert len(log_data) > 0
        assert log_data[-1]["cleanup_status"] == "completed"


# ═══════════════════════════════════════════════════════════
# 测试 2: execution_id 重复验证
# ═══════════════════════════════════════════════════════════

class TestFailure02_DuplicateExecutionId:

    def test_execution_id_is_db_autoincrement(self, tmp_path):
        """create_execution 使用 cur.lastrowid（DB auto-increment）"""
        db_path = str(tmp_path / "test_exec_id.db")
        create_test_db(db_path)
        seed_test_data(db_path)

        from app.executor.result_collector import ResultCollector
        collector = ResultCollector(db_path)

        exec1 = collector.create_execution(task_id=1, project_id=1, worker_id="w1")
        exec2 = collector.create_execution(task_id=2, project_id=1, worker_id="w2")
        collector.close()

        assert exec1.id != exec2.id, "execution_id 重复！"

    def test_concurrent_claim_is_atomic(self, tmp_path):
        """并发领取同一任务必须互斥"""
        db_path = str(tmp_path / "test_concurrent.db")
        create_test_db(db_path)
        seed_test_data(db_path)

        from app.executor.result_collector import ResultCollector
        success_count = [0]

        def claim_worker():
            collector = ResultCollector(db_path)
            claimed = collector.claim_task(task_id=1, worker_id=str(uuid.uuid4())[:8])
            if claimed:
                success_count[0] += 1
            collector.close()

        threads = [threading.Thread(target=claim_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert success_count[0] == 1, f"多个 Worker 同时领取了同一任务！{success_count[0]}"


# ═══════════════════════════════════════════════════════════
# 测试 3: executor_run 残留验证
# ═══════════════════════════════════════════════════════════

class TestFailure03_ResidualExecutorRun:

    def test_run_finalized_with_finished_at(self, tmp_path):
        """终态 run 必须有 finished_at"""
        db_path = str(tmp_path / "test_run.db")
        create_test_db(db_path)
        seed_test_data(db_path)

        from app.executor.run_store import RunStore
        store = RunStore(db_path)

        result = store.create_starting_run(project_id=1)
        assert result["success"]
        run_id = result["run"]["run_id"]

        store.finalize_run(run_id, "completed", finish_reason="all_completed")

        final_run = store.get_run_by_id(run_id)
        assert final_run["status"] == "completed"
        assert final_run["finished_at"] is not None, "finished_at 为空"

    def test_only_one_active_run_per_project(self, tmp_path):
        """同一项目同时只能有一个活跃 run"""
        db_path = str(tmp_path / "test_single_run.db")
        create_test_db(db_path)

        from app.executor.run_store import RunStore
        store = RunStore(db_path)

        r1 = store.create_starting_run(project_id=1)
        assert r1["success"]

        r2 = store.create_starting_run(project_id=1)
        assert not r2["success"], "应拒绝第二个活跃 run"


# ═══════════════════════════════════════════════════════════
# 测试 4: task_lease 残留验证
# ═══════════════════════════════════════════════════════════

class TestFailure04_ResidualTaskLease:

    def test_lease_released_on_completion(self, tmp_path):
        """finalize_execution 后 lease 必须 status='released'"""
        db_path = str(tmp_path / "test_lease.db")
        create_test_db(db_path)
        seed_test_data(db_path)

        from app.executor.result_collector import ResultCollector
        collector = ResultCollector(db_path)

        claimed = collector.claim_task(task_id=1, worker_id="w1", lease_seconds=60)
        assert claimed

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM task_leases WHERE task_id = 1 AND status = 'active'")
        assert cur.fetchone() is not None
        conn.close()

        from app.executor.cleanup import ExecutionFinalizer
        finalizer = ExecutionFinalizer(db_path)
        result = finalizer.finalize_execution(
            execution_id=0, task_id=1, exit_status="completed", worker_id="w1",
        )
        assert result["success"], f"finalize 失败: {result.get('errors')}"

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM task_leases WHERE task_id = 1 AND status = 'active'")
        assert cur.fetchone() is None, "lease 未释放"
        conn.close()

        collector.close()

    def test_lease_not_double_claimed(self, tmp_path):
        """已领取的任务不能被再次领取"""
        db_path = str(tmp_path / "test_lease2.db")
        create_test_db(db_path)
        seed_test_data(db_path)

        from app.executor.result_collector import ResultCollector
        collector = ResultCollector(db_path)

        assert collector.claim_task(task_id=2, worker_id="w1") is True
        assert collector.claim_task(task_id=2, worker_id="w2") is False
        collector.close()


# ═══════════════════════════════════════════════════════════
# 测试 5: Task C 重复 Bug + 状态机
# ═══════════════════════════════════════════════════════════

class TestFailure05_DuplicateBugAndStateMachine:

    def test_bug_fingerprint_dedup(self, tmp_path):
        """同一 task+execution+fingerprint 不应创建重复 Bug"""
        db_path = str(tmp_path / "test_bug.db")
        create_test_db(db_path)

        from app.executor.result_collector import ResultCollector
        collector = ResultCollector(db_path)

        bug_id_1 = collector.create_bug(
            project_id=1, task_id=1, execution_id=100,
            title="修复失败 Bug",
            error_message="ZeroDivisionError: division by zero",
            test_result="fail",
        )

        bug_id_2 = collector.create_bug(
            project_id=1, task_id=1, execution_id=100,
            title="修复失败 Bug（应去重）",
            error_message="ZeroDivisionError: division by zero",
            test_result="fail",
        )

        collector.close()
        assert bug_id_1 == bug_id_2, f"去重失败: {bug_id_1} != {bug_id_2}"

    def test_bug_different_fingerprint_allowed(self, tmp_path):
        """不同指纹应分别创建"""
        db_path = str(tmp_path / "test_bug2.db")
        create_test_db(db_path)

        from app.executor.result_collector import ResultCollector
        collector = ResultCollector(db_path)

        bug_1 = collector.create_bug(
            project_id=1, task_id=1, execution_id=100,
            title="Bug A", error_message="error A", test_result="fail",
        )
        bug_2 = collector.create_bug(
            project_id=1, task_id=1, execution_id=100,
            title="Bug B", error_message="error B - different", test_result="fail",
        )
        collector.close()
        assert bug_1 != bug_2

    def test_bug_state_machine_complete_flow(self, tmp_path):
        """Bug 状态机：reported→analyzing→analyzed→fix_ready→fixing→waiting_test→resolved"""
        db_path = str(tmp_path / "test_bug_sm.db")
        create_test_db(db_path)

        from app.executor.result_collector import ResultCollector
        collector = ResultCollector(db_path)

        bug_id = collector.create_bug(
            project_id=1, task_id=1, execution_id=100,
            title="状态机测试", error_message="测试", test_result="unknown",
        )

        # 完整合法流转
        collector.update_bug_status(bug_id, "analyzing", "开始分析")
        collector.update_bug_status(bug_id, "analyzed", "分析完成")
        collector.update_bug_status(bug_id, "fix_ready", "修复指令已生成")
        collector.update_bug_status(bug_id, "fixing", "开始修复")
        collector.update_bug_status(bug_id, "waiting_test", "等待测试")
        collector.update_bug_status(bug_id, "resolved", "测试通过")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM bug_status_logs WHERE bug_id = ?", (bug_id,))
        count = cur.fetchone()["c"]
        conn.close()

        assert count >= 6, f"状态日志不全: {count} 条"
        collector.close()


# ═══════════════════════════════════════════════════════════
# 测试 6: 辅助文件不污染
# ═══════════════════════════════════════════════════════════

class TestFailure06_AuxiliaryFilePollution:

    def test_dotgitignore_includes_executor_patterns(self, tmp_path):
        """.gitignore 必须包含 .executor/ 和相关模式"""
        gitignore_path = Path(__file__).resolve().parent.parent.parent / ".gitignore"
        if gitignore_path.exists():
            content = gitignore_path.read_text(encoding="utf-8")
            required = [".executor/", "worktree_cleanup_log.json", "merge_log.json"]
            for pattern in required:
                assert pattern in content, f".gitignore 缺少 '{pattern}'"


# ═══════════════════════════════════════════════════════════
# 测试 7: finalize_execution 幂等性
# ═══════════════════════════════════════════════════════════

class TestFailure07_IdempotentFinalize:

    def test_finalize_is_idempotent(self, tmp_path):
        """多次 finalize 不能产生副作用"""
        db_path = str(tmp_path / "test_idempotent.db")
        create_test_db(db_path)
        seed_test_data(db_path)

        from app.executor.cleanup import ExecutionFinalizer
        from app.executor.result_collector import ResultCollector

        collector = ResultCollector(db_path)
        exec_rec = collector.create_execution(task_id=1, project_id=1, worker_id="w1")
        exec_id = exec_rec.id
        collector.claim_task(task_id=1, worker_id="w1", lease_seconds=60)
        collector.close()

        finalizer = ExecutionFinalizer(db_path)
        r1 = finalizer.finalize_execution(
            execution_id=exec_id, task_id=1, exit_status="completed", worker_id="w1",
        )
        assert r1["success"], f"第一次 finalize 失败: {r1['errors']}"

        r2 = finalizer.finalize_execution(
            execution_id=exec_id, task_id=1, exit_status="completed", worker_id="w1",
        )
        fatal = [e for e in r2.get("errors", []) if "失败" in e]
        assert len(fatal) == 0, f"幂等 finalize 产生错误: {fatal}"

    def test_finalize_all_exit_statuses(self, tmp_path):
        """所有 10 种退出状态都应被正确处理"""
        db_path = str(tmp_path / "test_exit_statuses.db")
        create_test_db(db_path)
        seed_test_data(db_path)

        from app.executor.cleanup import ExecutionFinalizer
        from app.executor.result_collector import ResultCollector

        exit_statuses = [
            "completed", "blocked", "failed", "cancelled",
            "merge_conflict", "merge_regression_failed",
            "worker_lost", "timeout", "safety_violation", "shutdown",
        ]

        for i, status in enumerate(exit_statuses):
            collector = ResultCollector(db_path)
            exec_rec = collector.create_execution(task_id=1, project_id=1, worker_id=f"w{i}")
            exec_id = exec_rec.id
            collector.close()

            finalizer = ExecutionFinalizer(db_path)
            r = finalizer.finalize_execution(
                execution_id=exec_id, task_id=1,
                exit_status=status, worker_id=f"w{i}",
            )
            assert r["success"], f"exit_status={status} 失败: {r['errors']}"


# ═══════════════════════════════════════════════════════════
# 测试 8: MergeCoordinator 三场景
# ═══════════════════════════════════════════════════════════

class TestFailure08_MergeCoordinatorScenarios:

    def test_normal_merge(self, tmp_path):
        """场景一：正常合并"""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        os.system(f'cd "{repo_path}" && git init && git config user.email "t@t" && git config user.name "T" && git commit --allow-empty -m "init"')
        os.system(f'cd "{repo_path}" && git checkout -b executor/task-1-100')
        (repo_path / "file_a.txt").write_text("content A")
        os.system(f'cd "{repo_path}" && git add . && git commit -m "task 1"')
        os.system(f'cd "{repo_path}" && git checkout master')

        from app.executor.merge_coordinator import MergeCoordinator, MergeItem
        coordinator = MergeCoordinator(str(repo_path))

        item = MergeItem(
            task_id=1, execution_id=100,
            branch="executor/task-1-100", worktree_path="",
            worker_id="w1", start_commit=coordinator.get_master_commit() or "",
        )
        coordinator.enqueue(item)
        result = coordinator.process_next()
        assert result.get("task_id") == 1

    def test_real_conflict(self, tmp_path):
        """场景二：真实合并冲突"""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        os.system(f'cd "{repo_path}" && git init && git config user.email "t@t" && git config user.name "T"')
        (repo_path / "conflict.txt").write_text("line1\nline2\nline3")
        os.system(f'cd "{repo_path}" && git add . && git commit -m "init"')
        os.system(f'cd "{repo_path}" && git checkout -b executor/task-2-200')
        (repo_path / "conflict.txt").write_text("line1\nline2_MODIFIED\nline3")
        os.system(f'cd "{repo_path}" && git add . && git commit -m "task 2"')
        os.system(f'cd "{repo_path}" && git checkout master')
        (repo_path / "conflict.txt").write_text("line1\nline2_MASTER\nline3")
        os.system(f'cd "{repo_path}" && git add . && git commit -m "master change"')

        from app.executor.merge_coordinator import MergeCoordinator, MergeItem
        coordinator = MergeCoordinator(str(repo_path))

        item = MergeItem(
            task_id=2, execution_id=200,
            branch="executor/task-2-200", worktree_path="",
            worker_id="w2", start_commit=coordinator.get_master_commit() or "",
        )
        coordinator.enqueue(item)
        result = coordinator.process_next()
        assert result["status"] == "blocked" or "conflict" in result.get("error", "").lower()


# ═══════════════════════════════════════════════════════════
# 运行入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
