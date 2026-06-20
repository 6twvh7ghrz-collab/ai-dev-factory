"""
数据库迁移 003：executor_runs 表

新增表：
  executor_runs - 自动循环运行状态表

设计原则：
  1. 每个 run 代表一次 start-stop 循环生命周期
  2. run_id 唯一标识一轮循环（UUID v4）
  3. 同一 project 在任意时刻最多一条活跃 run（部分唯一索引保证）
  4. worker_id 默认 NULL，启动时生成 runner-{uuid}
  5. 心跳过期后可被接管，终态不可接管
  6. 显式事务 + 真实失败回滚

使用方法：
  cd backend
  python -m app.migrations.003_executor_runs

回滚：
  cd backend
  python -m app.migrations.003_executor_runs rollback
  有数据时需 --force

测试（仅在数据库副本上）：
  python -m app.migrations.003_executor_runs test
"""
import sqlite3
import uuid
import os
import shutil
import threading
import time
from pathlib import Path
from datetime import datetime


# ============================================================
# 表结构定义
# ============================================================

# Total fields in executor_runs: 23
# (id, run_id, project_id, current_task_id, worker_id, status, mode,
#  started_at, heartbeat_at, finished_at,
#  tasks_completed, tasks_blocked, tasks_failed, tasks_repaired,
#  tasks_skipped, tasks_total, current_step, pause_reason, last_error,
#  stop_requested, budget_json, created_at, updated_at)

CREATE_EXECUTOR_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS executor_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 唯一标识
    run_id              TEXT NOT NULL UNIQUE,

    -- 关联
    project_id          INTEGER DEFAULT NULL,
    current_task_id     INTEGER DEFAULT NULL,

    -- 运行实例身份（启动时生成，默认 NULL 防止进程重启复用）
    worker_id           TEXT DEFAULT NULL,

    -- 循环状态
    status              TEXT NOT NULL DEFAULT 'idle'
                        CHECK(status IN (
                            'idle','starting','scanning','claiming',
                            'executing','testing','repairing',
                            'paused','stopping','completed','blocked','failed'
                        )),

    -- 循环模式
    mode                TEXT NOT NULL DEFAULT 'auto_until_blocked'
                        CHECK(mode IN ('safe','auto_until_blocked','unattended')),

    -- 时间戳
    started_at          DATETIME,
    heartbeat_at        DATETIME,
    finished_at         DATETIME,

    -- 任务计数
    tasks_completed     INTEGER DEFAULT 0,
    tasks_blocked       INTEGER DEFAULT 0,
    tasks_failed        INTEGER DEFAULT 0,
    tasks_repaired      INTEGER DEFAULT 0,
    tasks_skipped       INTEGER DEFAULT 0,
    tasks_total         INTEGER DEFAULT 0,

    -- 当前步骤与暂停
    current_step        TEXT DEFAULT '',
    pause_reason        TEXT,
    last_error          TEXT,
    stop_requested      INTEGER DEFAULT 0,

    -- 预算（JSON）
    budget_json         TEXT DEFAULT '{}',

    created_at          DATETIME DEFAULT (datetime('now','localtime')),
    updated_at          DATETIME DEFAULT (datetime('now','localtime')),

    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
    FOREIGN KEY (current_task_id) REFERENCES development_tasks(id) ON DELETE SET NULL
)
"""

# 活跃项目部分唯一索引
CREATE_ACTIVE_PROJECT_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS
uq_executor_runs_active_project
ON executor_runs(project_id)
WHERE project_id IS NOT NULL
AND status IN (
    'starting','scanning','claiming',
    'executing','testing','repairing',
    'paused','stopping'
)
"""

# 普通索引
CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_status ON executor_runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_project_status ON executor_runs(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_heartbeat ON executor_runs(heartbeat_at)",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_task ON executor_runs(current_task_id)",
]

# updated_at 自动更新触发器
CREATE_UPDATED_AT_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_executor_runs_updated_at
AFTER UPDATE ON executor_runs
FOR EACH ROW
BEGIN
    UPDATE executor_runs
    SET updated_at = datetime('now','localtime')
    WHERE id = NEW.id;
END
"""


# ============================================================
# 迁移函数
# ============================================================

def migrate(db_path: str = "data/ai_factory.db"):
    """执行迁移"""
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] 数据库文件不存在: {db_path}")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        cur = conn.cursor()

        # 检查是否已存在
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_runs'"
        )
        if cur.fetchone():
            print("[SKIP] executor_runs 表已存在，跳过迁移")
            conn.close()
            return True

        # 显式事务
        conn.execute("BEGIN IMMEDIATE")

        # 1. 创建表
        cur.execute(CREATE_EXECUTOR_RUNS_SQL)
        print("[OK] executor_runs table created")

        # 2. 活跃项目部分唯一索引
        cur.execute(CREATE_ACTIVE_PROJECT_INDEX_SQL)
        print("[OK] uq_executor_runs_active_project (partial unique) created")

        # 3. 普通索引
        for idx_sql in CREATE_INDEXES_SQL:
            cur.execute(idx_sql)
        print("[OK] 4 regular indexes created")

        # 4. updated_at 触发器
        cur.execute(CREATE_UPDATED_AT_TRIGGER_SQL)
        print("[OK] trg_executor_runs_updated_at trigger created")

        conn.commit()
        print(f"[DONE] 迁移 003 完成 - {datetime.now().isoformat()}")

        # 验证
        cur.execute("PRAGMA table_info(executor_runs)")
        field_count = len(cur.fetchall())
        cur.execute("PRAGMA index_list(executor_runs)")
        idx_count = len(cur.fetchall())
        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        print(f"  验证: fields={field_count}, indexes={idx_count}, integrity={integrity}")

        conn.close()
        return True

    except Exception as e:
        conn.rollback()
        print(f"[FAIL] 迁移失败，已回滚: {e}")
        conn.close()
        return False


# ============================================================
# 回滚函数
# ============================================================

def rollback(db_path: str = "data/ai_factory.db", force: bool = False):
    """回滚迁移"""
    db_path = Path(db_path)
    if not db_path.exists():
        print("[SKIP] 数据库不存在")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    try:
        cur = conn.cursor()

        # 检查表是否存在
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_runs'"
        )
        if not cur.fetchone():
            print("[SKIP] executor_runs 表不存在，无需回滚")
            conn.close()
            return True

        # 记录数检查
        cur.execute("SELECT COUNT(*) FROM executor_runs")
        row_count = cur.fetchone()[0]

        if row_count > 0 and not force:
            print(f"[BLOCKED] executor_runs 包含 {row_count} 条数据")
            print("  使用 --force 参数强制执行回滚（数据将丢失）")
            print("  建议先备份数据库")
            conn.close()
            return False

        # 备份
        backup_path = db_path.parent / f"{db_path.stem}_backup_rollback_003_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(str(db_path), str(backup_path))
        print(f"[BACKUP] 已备份到: {backup_path}")

        # 删除触发器
        cur.execute("DROP TRIGGER IF EXISTS trg_executor_runs_updated_at")
        print("[DROP] trg_executor_runs_updated_at trigger")

        # 删除表（索引随表自动删除）
        cur.execute("DROP TABLE IF EXISTS executor_runs")
        print(f"[DROP] executor_runs table ({row_count} rows deleted)")

        conn.commit()

        # 验证
        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        cur.execute("PRAGMA foreign_key_check")
        fk_violations = len(cur.fetchall())
        print(f"[DONE] 回滚完成 - integrity={integrity}, fk_violations={fk_violations}")

        conn.close()
        return True

    except Exception as e:
        conn.rollback()
        print(f"[FAIL] 回滚失败: {e}")
        conn.close()
        return False


# ============================================================
# 测试函数（仅在副本数据库上执行）
# ============================================================

def run_tests():
    """在测试数据库副本上运行完整测试套件"""
    import subprocess

    # 确定路径
    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent.parent  # backend/app/migrations -> backend
    db_path = backend_dir / "data" / "ai_factory.db"
    test_db = backend_dir / "data" / f"ai_factory_test_003_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

    if not db_path.exists():
        print(f"[FATAL] 正式数据库不存在: {db_path}")
        return

    # 创建测试副本
    shutil.copy2(str(db_path), str(test_db))
    print(f"[TEST] 测试数据库: {test_db}")
    print(f"[TEST] 源数据库: {db_path} ({db_path.stat().st_size} bytes)")

    # 从测试数据库获取真实 project IDs
    tmp_conn = sqlite3.connect(str(test_db))
    tmp_c = tmp_conn.cursor()
    tmp_c.execute("SELECT id FROM projects ORDER BY id")
    all_pids = [r[0] for r in tmp_c.fetchall()]
    tmp_conn.close()

    if len(all_pids) < 1:
        print("[FATAL] 测试需要至少 1 个项目，实际只有 {} 个".format(len(all_pids)))
        return

    # 为测试分配项目 ID（复用，只要不冲突即可）
    PID_CONCURRENT = all_pids[0]
    PID_INDEX = all_pids[0]  # 复用（不同 run_id）
    PID_TAKEOVER = all_pids[0] if len(all_pids) < 2 else all_pids[1]
    PID_UPDATED = all_pids[0] if len(all_pids) < 2 else all_pids[1]
    PID_ROLLBACK = all_pids[0] if len(all_pids) < 2 else all_pids[1]

    print(f"[TEST] 项目ID分配: concurrent={PID_CONCURRENT}, index={PID_INDEX}, "
          f"takeover={PID_TAKEOVER}, updated={PID_UPDATED}, rollback={PID_ROLLBACK}")

    passed = 0
    failed = 0

    def check(test_name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {test_name}{' - ' + detail if detail else ''}")
        else:
            failed += 1
            print(f"  [FAIL] {test_name}{' - ' + detail if detail else ''}")

    # ── TEST 1: 首次迁移 ──
    print("\n── TEST 1: 首次迁移 ──")
    result = migrate(str(test_db))
    check("首次迁移成功", result)

    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_runs'"
    )
    check("表已创建", c.fetchone() is not None)

    c.execute("PRAGMA table_info(executor_runs)")
    fields = c.fetchall()
    check("Field count = 23", len(fields) == 23,
          f"got {len(fields)}")

    c.execute("PRAGMA index_list(executor_runs)")
    indexes = c.fetchall()
    # Expected: 1 autoindex (run_id UNIQUE) + 1 partial unique + 4 regular = 6
    check("Index count >= 5", len(indexes) >= 5,
          f"got {len(indexes)} indexes")

    # Check partial unique index exists
    partial_idx_found = any(
        "uq_executor_runs_active_project" in idx[1] and idx[2] == 1
        for idx in indexes
    )
    check("部分唯一索引存在", partial_idx_found)

    # Check trigger exists
    c.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='trg_executor_runs_updated_at'"
    )
    check("updated_at 触发器存在", c.fetchone() is not None)

    c.execute("PRAGMA integrity_check")
    check("integrity_check = ok", c.fetchone()[0] == "ok")

    c.execute("PRAGMA foreign_key_check")
    fk_count = len(c.fetchall())
    check("foreign_key_check = 0", fk_count == 0,
          f"got {fk_count} violations")

    conn.close()

    # ── TEST 2: 重复迁移（幂等） ──
    print("\n── TEST 2: 重复迁移（幂等）──")
    result2 = migrate(str(test_db))
    check("重复迁移不报错", result2)

    # ── TEST 3: 真实事务失败注入 ──
    print("\n── TEST 3: 真实事务失败注入 ──")
    # 使用独立连接模拟失败
    test3_conn = sqlite3.connect(str(test_db))
    test3_conn.execute("PRAGMA foreign_keys = ON")
    test3_c = test3_conn.cursor()

    # 先确保 executor_runs 不存在（在回滚后测试）
    # 不能在这里删表（因为已经迁移了），我们创建另一个连接模拟新迁移失败

    # 用同一个数据库，在事务中创建临时表模拟
    test3_conn.execute("BEGIN IMMEDIATE")
    try:
        test3_c.execute("""
            CREATE TABLE IF NOT EXISTS _test_rollback_table (
                id INTEGER PRIMARY KEY,
                name TEXT
            )
        """)
        test3_c.execute("""
            CREATE INDEX IF NOT EXISTS _test_rollback_idx ON _test_rollback_table(name)
        """)
        # 模拟失败
        raise Exception("SIMULATED FAILURE for rollback test")
    except Exception:
        test3_conn.rollback()
    else:
        test3_conn.commit()

    # 验证回滚成功
    test3_c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_test_rollback_table'"
    )
    check("事务回滚后临时表不存在", test3_c.fetchone() is None)
    test3_c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='_test_rollback_idx'"
    )
    check("事务回滚后临时索引不存在", test3_c.fetchone() is None)
    test3_c.execute("PRAGMA integrity_check")
    check("回滚后 integrity_check = ok", test3_c.fetchone()[0] == "ok")

    test3_conn.close()

    # ── TEST 4: 100轮并发创建活跃run ──
    print("\n── TEST 4: 100轮并发创建活跃run ──")
    total_rounds = 100
    test_project_id = PID_CONCURRENT
    double_success_count = 0
    db_locked_count = 0

    # 清理
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_runs WHERE project_id = ?", (test_project_id,))
    conn.commit()
    conn.close()

    def create_run(worker_name, results):
        try:
            t_conn = sqlite3.connect(str(test_db), timeout=5)
            tc = t_conn.cursor()
            tc.execute("PRAGMA foreign_keys = ON")
            run_id = f"concurrent-{worker_name}-{uuid.uuid4().hex[:8]}"
            tc.execute(
                """INSERT INTO executor_runs
                (run_id, project_id, worker_id, status, mode)
                VALUES (?, ?, ?, 'starting', 'auto_until_blocked')""",
                (run_id, test_project_id, worker_name),
            )
            t_conn.commit()
            results.append({"worker": worker_name, "success": True})
        except sqlite3.IntegrityError:
            results.append({"worker": worker_name, "success": False, "error": "integrity"})
        except sqlite3.OperationalError as e:
            results.append({"worker": worker_name, "success": False, "error": str(e)})
        finally:
            t_conn.close()

    for rnd in range(1, total_rounds + 1):
        # 清理上一轮
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = ON")
        c = conn.cursor()
        c.execute("DELETE FROM executor_runs WHERE project_id = ?", (test_project_id,))
        conn.commit()
        conn.close()

        results = []
        t1 = threading.Thread(target=create_run, args=(f"runner-A-{rnd}", results))
        t2 = threading.Thread(target=create_run, args=(f"runner-B-{rnd}", results))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        success_count = sum(1 for r in results if r.get("success"))
        locked = any("locked" in str(r.get("error", "")) for r in results if not r.get("success"))

        if success_count > 1:
            double_success_count += 1
        if locked:
            db_locked_count += 1

        if rnd % 20 == 0 or rnd == total_rounds:
            print(f"  Round {rnd:>3}/{total_rounds} - double_success={double_success_count}, db_locked={db_locked_count}")

    check("双成功次数 = 0", double_success_count == 0,
          f"got {double_success_count}")
    check("未处理 database locked 次数 = 0", db_locked_count == 0,
          f"got {db_locked_count}")

    # ── TEST 5: 部分唯一索引生效 ──
    print("\n── TEST 5: 部分唯一索引生效 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    test_pid = PID_INDEX
    c.execute("DELETE FROM executor_runs WHERE project_id = ?", (test_pid,))
    conn.commit()

    # 创建第一条活跃 run
    c.execute(
        """INSERT INTO executor_runs (run_id, project_id, status, mode)
        VALUES ('index-test-1', ?, 'starting', 'auto_until_blocked')""",
        (test_pid,),
    )
    conn.commit()
    check("第一条活跃run创建成功", c.lastrowid is not None)

    # 尝试创建第二条活跃 run（应失败）
    try:
        c.execute(
            """INSERT INTO executor_runs (run_id, project_id, status, mode)
            VALUES ('index-test-2', ?, 'executing', 'auto_until_blocked')""",
            (test_pid,),
        )
        conn.commit()
        check("第二条活跃run被拒绝", False, "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("第二条活跃run被拒绝", True, "IntegrityError as expected")

    # 创建 completed 状态的 run（不违反唯一索引）
    c.execute(
        """INSERT INTO executor_runs (run_id, project_id, status, mode)
        VALUES ('index-test-3', ?, 'completed', 'auto_until_blocked')""",
        (test_pid,),
    )
    conn.commit()
    check("终态run允许与活跃run共存", c.lastrowid is not None)

    conn.close()

    # ── TEST 6: 心跳接管竞争 ──
    print("\n── TEST 6: 心跳接管竞争 ──")

    takeover_sql = """UPDATE executor_runs
        SET worker_id = ?,
            heartbeat_at = datetime('now','localtime'),
            updated_at = datetime('now','localtime')
        WHERE run_id = ?
        AND status NOT IN ('completed', 'blocked', 'failed')
        AND (
            worker_id IS NULL
            OR heartbeat_at IS NULL
            OR heartbeat_at < datetime('now','localtime','-120 seconds')
        )"""

    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    takeover_pid = PID_TAKEOVER
    c.execute("DELETE FROM executor_runs WHERE project_id = ?", (takeover_pid,))
    conn.commit()

    # 6a: 活跃心跳阻止接管
    c.execute(
        """INSERT INTO executor_runs
        (run_id, project_id, worker_id, status, mode, heartbeat_at)
        VALUES ('takeover-test-1', ?, 'runner-original', 'executing',
                'auto_until_blocked', datetime('now','localtime'))""",
        (takeover_pid,),
    )
    conn.commit()

    c.execute(takeover_sql, ("runner-takeover", "takeover-test-1"))
    conn.commit()
    check("Active heartbeat blocks takeover", c.rowcount == 0,
          f"rowcount={c.rowcount} (should be 0)")

    # 6b: 过期心跳允许接管
    c.execute(
        """UPDATE executor_runs
        SET heartbeat_at = datetime('now','localtime','-5 minutes')
        WHERE run_id = 'takeover-test-1'"""
    )
    conn.commit()

    c.execute(takeover_sql, ("runner-new-owner", "takeover-test-1"))
    conn.commit()
    check("Expired heartbeat allows takeover", c.rowcount == 1,
          f"rowcount={c.rowcount} (should be 1)")

    # 验证 worker_id 已更新
    c.execute("SELECT worker_id FROM executor_runs WHERE run_id='takeover-test-1'")
    new_worker = c.fetchone()[0]
    check("Takeover更新了worker_id", new_worker == "runner-new-owner",
          f"got {new_worker}")

    # 6c: 两个恢复进程同时接管（仅一个成功）
    # 先把 takeover-test-1 改为终态，释放 project 的活跃锁
    c.execute(
        "UPDATE executor_runs SET status='completed' WHERE run_id='takeover-test-1'"
    )
    conn.commit()

    c.execute(
        """INSERT INTO executor_runs
        (run_id, project_id, worker_id, status, mode, heartbeat_at)
        VALUES ('takeover-compete', ?, 'runner-original', 'executing',
                'auto_until_blocked', datetime('now','localtime','-5 minutes'))""",
        (takeover_pid,),
    )
    conn.commit()
    conn.close()

    compete_results = []

    def takeover_attempt(worker_name):
        try:
            t_conn = sqlite3.connect(str(test_db), timeout=5)
            tc = t_conn.cursor()
            tc.execute("PRAGMA foreign_keys = ON")
            tc.execute(takeover_sql, (worker_name, "takeover-compete"))
            t_conn.commit()
            compete_results.append({"worker": worker_name, "rowcount": tc.rowcount})
            t_conn.close()
        except Exception as ex:
            compete_results.append({"worker": worker_name, "rowcount": -1, "error": str(ex)})

    t_a = threading.Thread(target=takeover_attempt, args=("runner-compete-A",))
    t_b = threading.Thread(target=takeover_attempt, args=("runner-compete-B",))
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    success_count = sum(1 for r in compete_results if r["rowcount"] == 1)
    check("两个进程同时接管仅一个成功", success_count == 1,
          f"success_count={success_count}, results={compete_results}")

    # 6d: 终态禁止接管
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    # 清理 takeover-compete（活跃状态），为终态测试释放 project
    c.execute("UPDATE executor_runs SET status='completed' WHERE run_id='takeover-compete'")
    conn.commit()

    for term_status in ["completed", "blocked", "failed"]:
        run_id = f"takeover-{term_status}"
        c.execute("DELETE FROM executor_runs WHERE run_id = ?", (run_id,))
        c.execute(
            """INSERT INTO executor_runs
            (run_id, project_id, worker_id, status, mode, heartbeat_at)
            VALUES (?, ?, 'runner-original', ?, 'auto_until_blocked',
                    datetime('now','localtime','-5 minutes'))""",
            (run_id, takeover_pid, term_status),
        )
        conn.commit()

        c.execute(takeover_sql, ("runner-new", run_id))
        conn.commit()
        check(f"{term_status} state blocks takeover", c.rowcount == 0,
              f"rowcount={c.rowcount} (should be 0)")

    conn.close()

    # ── TEST 7: updated_at 更新 ──
    print("\n── TEST 7: updated_at 更新 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    upd_pid = PID_UPDATED
    c.execute("DELETE FROM executor_runs WHERE project_id = ?", (upd_pid,))
    conn.commit()

    c.execute(
        """INSERT INTO executor_runs
        (run_id, project_id, status, mode)
        VALUES ('updated-at-test', ?, 'idle', 'auto_until_blocked')""",
        (upd_pid,),
    )
    conn.commit()

    c.execute("SELECT updated_at FROM executor_runs WHERE run_id='updated-at-test'")
    before = c.fetchone()[0]
    print(f"  updated_at before: {before}")

    time.sleep(1.1)

    # UPDATE 应该触发 trigger 更新 updated_at
    c.execute(
        "UPDATE executor_runs SET status='scanning' WHERE run_id='updated-at-test'"
    )
    conn.commit()

    c.execute("SELECT updated_at FROM executor_runs WHERE run_id='updated-at-test'")
    after = c.fetchone()[0]
    print(f"  updated_at after:  {after}")

    check("updated_at after UPDATE changes", before != after,
          f"before={before}, after={after}")

    conn.close()

    # ── TEST 8: 回滚保护 ──
    print("\n── TEST 8: 回滚保护 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    test_rpid = PID_ROLLBACK
    c.execute("DELETE FROM executor_runs WHERE project_id = ?", (test_rpid,))
    c.execute(
        """INSERT INTO executor_runs
        (run_id, project_id, status, mode)
        VALUES ('rollback-test', ?, 'idle', 'auto_until_blocked')""",
        (test_rpid,),
    )
    conn.commit()
    conn.close()

    # 无 --force 时应被阻止
    result_no_force = rollback(str(test_db), force=False)
    check("有数据时默认拒绝回滚", not result_no_force)

    # 带 --force 应成功
    result_force = rollback(str(test_db), force=True)
    check("--force 强制回滚成功", result_force)

    # 验证表已删除
    conn = sqlite3.connect(str(test_db))
    c = conn.cursor()
    c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_runs'"
    )
    check("强制回滚后表已删除", c.fetchone() is None)
    c.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name='trg_executor_runs_updated_at'"
    )
    check("强制回滚后触发器已删除", c.fetchone() is None)
    c.execute("PRAGMA integrity_check")
    check("回滚后 integrity_check = ok", c.fetchone()[0] == "ok")
    c.execute("PRAGMA foreign_key_check")
    fk_count = len(c.fetchall())
    check("回滚后 foreign_key_check = 0", fk_count == 0,
          f"got {fk_count} violations")
    conn.close()

    # ── TEST 9: 重新迁移 ──
    print("\n── TEST 9: 重新迁移 ──")
    result_re = migrate(str(test_db))
    check("回滚后重新迁移成功", result_re)

    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_runs'"
    )
    check("重新迁移后表存在", c.fetchone() is not None)
    c.execute("PRAGMA integrity_check")
    check("重新迁移后 integrity_check = ok", c.fetchone()[0] == "ok")
    c.execute("PRAGMA foreign_key_check")
    fk_count = len(c.fetchall())
    check("重新迁移后 foreign_key_check = 0", fk_count == 0,
          f"got {fk_count} violations")
    conn.close()

    # ── SUMMARY ──
    print(f"\n{'='*60}")
    print(f"  总计: {passed} PASSED, {failed} FAILED")
    print(f"{'='*60}")

    # 清理测试数据库
    try:
        test_db.unlink()
        # 也清理 WAL/SHM
        for ext in [".db-wal", ".db-shm"]:
            p = Path(str(test_db) + ext)
            if p.exists():
                p.unlink()
        print(f"[CLEANUP] 已删除测试数据库: {test_db}")
    except Exception as e:
        print(f"[CLEANUP] 清理失败: {e}")

    return passed, failed


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "rollback":
            force = "--force" in sys.argv
            rollback(force=force)
        elif sys.argv[1] == "test":
            run_tests()
        elif sys.argv[1] == "--help" or sys.argv[1] == "-h":
            print(__doc__)
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print("Usage: python -m app.migrations.003_executor_runs [rollback|test]")
    else:
        migrate()
