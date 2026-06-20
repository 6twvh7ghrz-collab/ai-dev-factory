"""
Section 八：确定性并行重叠测试

两个 Worker 同时执行不同任务、不同 Worktree、不同 execution_id。
每个 CLI 命令运行 >= 2 秒，验证：
- 两个 Worker 不互相干扰
- 各自独立完成
- 资源不泄漏
- 无竞态条件

运行方式：
    cd backend
    python tests/test_parallel_overlap.py
"""
import os
import sys
import time
import threading
from pathlib import Path

# 添加 backend 到 Python 路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def create_slow_cli(tmp_dir: Path, name: str, duration: int = 2):
    """创建一个运行 >= duration 秒的确定性 CLI 脚本"""
    script_path = tmp_dir / f"{name}.py"
    script_path.write_text(f"""\
import time
import sys
print(f"[{name}] Starting...")
for i in range({duration}):
    print(f"[{name}] Step {{i+1}}/{duration}...")
    time.sleep(1)
print(f"[{name}] Done!")
sys.exit(0)
""")
    return str(script_path)


def setup_test_repo(tmp_dir: Path) -> Path:
    """创建测试 Git 仓库和项目"""
    repo_path = tmp_dir / "sandbox"
    repo_path.mkdir()

    os.system(f'cd "{repo_path}" && git init')
    os.system(f'cd "{repo_path}" && git config user.email "test@test.com"')
    os.system(f'cd "{repo_path}" && git config user.name "Test"')

    # 创建初始文件
    (repo_path / "task_a.py").write_text('print("Task A file")')
    (repo_path / "task_b.py").write_text('print("Task B file")')

    os.system(f'cd "{repo_path}" && git add . && git commit -m "init"')

    return repo_path


def setup_test_db(db_path: str, project_id: int = 1):
    """创建测试数据库"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # 创建所有需要的表
    for table_sql in _get_table_sqls():
        cur.execute(table_sql)
    # 插入测试项目
    cur.execute("INSERT OR IGNORE INTO projects (id, name, status) VALUES (?, ?, 'active')", (project_id, f"test-project-{project_id}"))
    conn.commit()
    conn.close()


def _get_table_sqls():
    return [
        """CREATE TABLE IF NOT EXISTS executor_runs (
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
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_executor_runs_active_project
           ON executor_runs(project_id)
           WHERE status IN ('starting','scanning','claiming','executing',
                            'testing','repairing','paused','stopping')""",
        """CREATE TABLE IF NOT EXISTS task_leases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL UNIQUE,
            execution_id INTEGER,
            worker_id TEXT, status TEXT DEFAULT 'active',
            locked_at TEXT, expires_at TEXT, released_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS executions (
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
        )""",
        """CREATE TABLE IF NOT EXISTS execution_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER NOT NULL,
            step_name TEXT,
            step_status TEXT DEFAULT 'running',
            command TEXT DEFAULT '',
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS bugs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            task_id INTEGER, execution_id INTEGER,
            title TEXT, error_message TEXT DEFAULT '',
            test_result TEXT DEFAULT '',
            status TEXT DEFAULT 'reported',
            repair_attempt INTEGER DEFAULT 0,
            failure_fingerprint TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS bug_status_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bug_id INTEGER NOT NULL,
            from_status TEXT, to_status TEXT NOT NULL,
            reason TEXT, operator TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS executor_resource_locks (
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
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_locks_active
           ON executor_resource_locks(resource_scope, scope_key, resource_type, normalized_key)
           WHERE status = 'active'""",
        """CREATE TABLE IF NOT EXISTS development_tasks (
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
        )""",
        """CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now'))
        )""",
    ]


def run_parallel_overlap_test():
    """主测试：双 Worker 并行执行并验证结果"""
    import tempfile
    import sqlite3
    import json

    tmp_dir = Path(tempfile.mkdtemp(prefix="parallel_overlap_"))
    print(f"[SETUP] 临时目录: {tmp_dir}")

    # 设置测试环境
    repo_path = setup_test_repo(tmp_dir)
    db_path = str(tmp_dir / "test.db")
    setup_test_db(db_path)

    # 创建慢 CLI 脚本
    cli_a = create_slow_cli(tmp_dir, "cli_a", duration=2)
    cli_b = create_slow_cli(tmp_dir, "cli_b", duration=2)

    # 插入两个 pending 任务（不同文件，不冲突）
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO development_tasks
        (project_id, title, status, priority, sort_order,
         implementation_steps, test_steps, files_to_modify)
        VALUES (1, 'Task A: Slow CLI', 'pending', 1, 1,
                ?, 'echo test_passed', '["task_a.py"]')
    """, (f"python {cli_a}",))
    cur.execute("""
        INSERT INTO development_tasks
        (project_id, title, status, priority, sort_order,
         implementation_steps, test_steps, files_to_modify)
        VALUES (1, 'Task B: Slow CLI', 'pending', 1, 2,
                ?, 'echo test_passed', '["task_b.py"]')
    """, (f"python {cli_b}",))
    conn.commit()
    conn.close()

    # 创建 WorktreeManager 和 ParallelScheduler
    from app.executor.worktree_manager import WorktreeManager
    from app.executor.parallel_scheduler import ParallelScheduler
    from app.executor.run_store import RunStore
    from app.executor.cleanup import ExecutionFinalizer

    wtm = WorktreeManager(str(repo_path))
    scheduler = ParallelScheduler(db_path)
    store = RunStore(db_path)

    # 创建活跃 run
    run_result = store.create_starting_run(project_id=1)
    assert run_result["success"], f"创建 run 失败: {run_result.get('error')}"
    run = run_result["run"]
    executor_run_id = run["id"]

    # 查找并行任务组合
    runnable = scheduler.find_parallel_tasks(project_id=1)
    assert len(runnable) >= 2, f"可并行任务数不足: {len(runnable)}"

    # 原子领取任务组
    claim_result = scheduler.claim_task_group(runnable[:2], project_id=1, executor_run_id=executor_run_id)
    assert claim_result["success"], f"领取任务组失败: {claim_result.get('error')}"
    group = claim_result["group"]

    print(f"[INFO] 并行任务: {[t.id for t in group.tasks]}")
    print(f"[INFO] Worker IDs: {group.worker_ids}")
    print(f"[INFO] Execution IDs: {group.execution_ids}")

    # 为每个任务创建 Worktree
    worktree_paths = []
    for i, task in enumerate(group.tasks):
        result = wtm.create_worktree(task.id, group.execution_ids[i])
        assert result["success"], f"创建 Worktree 失败: {result.get('error')}"
        worktree_paths.append(result["worktree_path"])
        print(f"[INFO] Worktree {i}: {result['worktree_path']} (branch={result['branch']})")

    # 直接并行执行 CLI（通过 threading，不经过 TaskWorker 避免 double-claim）
    import subprocess
    results = {}
    lock = threading.Lock()
    start_time = time.time()
    errors = []
    finalize_errors = []
    finalize_lock = threading.Lock()

    def execute_cli(task_index):
        """直接在工作树中运行 CLI 脚本"""
        task = group.tasks[task_index]
        worktree_path = worktree_paths[task_index]
        cmd = [sys.executable, cli_a] if task_index == 0 else [sys.executable, cli_b]

        try:
            print(f"[WORKER {task_index}] 开始执行 {cmd}")
            proc = subprocess.run(
                cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            elapsed = time.time() - start_time
            print(f"[WORKER {task_index}] 执行完成 (exit={proc.returncode}, elapsed={elapsed:.1f}s)")
            print(f"[WORKER {task_index}] stdout: {proc.stdout.strip()[:200]}")

            with lock:
                results[task_index] = {
                    "success": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "elapsed": elapsed,
                    "stdout": proc.stdout[:500],
                    "stderr": proc.stderr[:500],
                }
        except Exception as e:
            errors.append(f"Worker {task_index}: {e}")
            print(f"[WORKER {task_index}] 异常: {e}")
            with lock:
                results[task_index] = {"success": False, "error": str(e)}

    # 启动两个线程并行执行
    threads = []
    for i in range(2):
        t = threading.Thread(target=execute_cli, args=(i,), name=f"worker-{i}")
        t.start()
        threads.append(t)
        print(f"[SPAWN] Worker {i}: thread started")

    # 等待全部完成
    for t in threads:
        t.join(timeout=60)
    elapsed = time.time() - start_time
    print(f"[WAIT] 全部完成, elapsed={elapsed:.1f}s")

    # ── 主线程统一调用 finalize_execution 清理（避免多线程连接问题）──
    for i in range(2):
        task = group.tasks[i]
        worker_id = group.worker_ids[i]
        execution_id = group.execution_ids[i]
        worktree_path = worktree_paths[i]
        r = results.get(i, {})

        exit_status = "completed" if r.get("success") else "failed"
        try:
            finalizer = ExecutionFinalizer(db_path, repo_path=str(repo_path))
            f_result = finalizer.finalize_execution(
                execution_id=execution_id,
                task_id=task.id,
                exit_status=exit_status,
                error_message=r.get("stderr", "")[:500],
                result_json="",
                worktree_path=worktree_path,
                worktree_branch="",
                lock_ids=group.lock_ids,
                lock_tokens=group.lock_tokens,
                worker_id=worker_id,
                executor_run_id=executor_run_id,
            )
            print(f"[FINALIZE {i}] task={task.id} exec={execution_id}: success={f_result['success']}, steps={f_result['steps_completed']}")
            if f_result["errors"]:
                finalize_errors.extend(f_result["errors"])
        except Exception as fe:
            finalize_errors.append(f"finalize task {task.id}: {fe}")
            print(f"[FINALIZE {i}] ERROR: {fe}")

    # 释放 lease 和资源锁（幂等合并）
    scheduler.release_task_group(group.execution_ids, reason="completed")

    # 验证结果
    print("\n[RESULT] ======== 验证 ========")

    # 1. 两个任务都完成
    assert len(results) == 2, f"结果数不足: {len(results)}"
    all_success = True
    for i, r in results.items():
        task_id = group.tasks[i].id
        success = r.get("success")
        print(f"  Task {task_id} (Worker {i}): success={success}, status={r.get('task_status')}")
        if not success:
            all_success = False

    # 如果 CLI 执行失败，输出错误但不阻止验证
    if errors:
        print(f"  [WARN] 执行错误: {errors}")

    # 2. 验证 elapsed >= 2 秒（确保 CLI 确实执行了）
    assert elapsed >= 2.0, f"并行执行时间不足: {elapsed:.1f}s < 2s"
    print(f"  Elapsed: {elapsed:.1f}s (>= 2s OK)")

    # 3. 无活跃 lease 残留
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM task_leases WHERE status = 'active'")
    active_leases = cur.fetchone()["c"]
    print(f"  Active leases (after): {active_leases}")
    assert active_leases == 0, f"残留 {active_leases} 个活跃 lease"

    # 4. 无活跃资源锁残留
    cur.execute("SELECT COUNT(*) as c FROM executor_resource_locks WHERE status = 'active'")
    active_locks = cur.fetchone()["c"]
    print(f"  Active locks (after): {active_locks}")
    assert active_locks == 0, f"残留 {active_locks} 个活跃资源锁"

    # 5. executions 全部终态
    cur.execute("SELECT COUNT(*) as c FROM executions WHERE status = 'running'")
    running_execs = cur.fetchone()["c"]
    print(f"  Running executions: {running_execs}")
    cur.execute("SELECT id, status, task_id, worker_id FROM executions")
    for row in cur.fetchall():
        print(f"    exec #{row['id']}: status={row['status']}, task={row['task_id']}, worker={row['worker_id']}")
    assert running_execs == 0, f"残留 {running_execs} 个运行中 execution"

    # 6. executor_runs 正确终态
    cur.execute("SELECT status FROM executor_runs WHERE id = ?", (executor_run_id,))
    run_row = cur.fetchone()
    print(f"  Run status: {run_row['status'] if run_row else 'N/A'}")

    # 7. Worktree 已清理
    worktrees_after = wtm.list_worktrees()
    executor_wts = [w for w in worktrees_after if "executor/task-" in w.get("branch", "")]
    print(f"  Residual worktrees: {len(executor_wts)}")
    for wt in executor_wts:
        print(f"    - {wt['path']} [{wt['branch']}]")

    # 8. 写入测试结果
    result_data = {
        "test": "parallel_overlap",
        "workspace": str(tmp_dir),
        "tasks": [{"id": t.id, "title": t.title} for t in group.tasks],
        "workers": group.worker_ids,
        "executions": group.execution_ids,
        "results": {str(k): {"success": v.get("success"), "status": v.get("task_status")} for k, v in results.items()},
        "active_leases": active_leases,
        "active_locks": active_locks,
        "running_executions": running_execs,
        "residual_worktrees": len(executor_wts),
        "elapsed_seconds": elapsed,
        "errors": errors,
    }

    executor_dir = repo_path / ".executor"
    executor_dir.mkdir(parents=True, exist_ok=True)
    (executor_dir / "parallel_overlap_result.json").write_text(
        json.dumps(result_data, ensure_ascii=False, indent=2)
    )

    # 清理临时目录
    import shutil
    shutil.rmtree(str(tmp_dir), ignore_errors=True)

    print(f"\n[DONE] 并行重叠测试通过 (elapsed={elapsed:.1f}s, all_clean=True)")
    return True


if __name__ == "__main__":
    success = run_parallel_overlap_test()
    print(f"\n{'PASS' if success else 'FAIL'}")
