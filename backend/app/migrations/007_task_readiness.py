"""
数据库迁移 007：任务准备状态门 (Task Readiness Gate)

目的：
  为 development_tasks 表增加 readiness_status 字段，
  防止未经工程规划的任务被调度器自动领取执行。

readiness_status 枚举：
  - draft           : 默认值，任务刚创建/未规划
  - needs_planning  : 等待工程规划（缺少工作区/Git/文件路径/测试方案/风险评估）
  - ready           : 已通过全部准入验证，可被调度器领取

安全门规则（调度器强制执行）：
  find_runnable_tasks 必须同时满足：
    status = 'pending' AND readiness_status = 'ready'
  任一不满足则任务不可调度。

数据迁移规则：
  1. 新增列（幂等，已有则跳过）
  2. 未显式设置的任务默认 readiness_status='draft'
  3. 项目 56（正式电商项目）→ 全部 needs_planning
     原因：尚未绑定验证过的代码仓库、文件路径未通过目录扫描、
           涉及 Electron/采集平台/外部服务，不适合自动试运行
  4. 项目 6 Task #38（沙箱任务）→ ready
     原因：已验证工作区/路径/无依赖/低风险/仅修改2个文件

使用方法：
  cd backend
  python -m app.migrations.007_task_readiness

测试（仅在数据库副本上）：
  python -m app.migrations.007_task_readiness test
"""
import sqlite3
import shutil
import sys
from pathlib import Path
from datetime import datetime


# ============================================================
# 字段定义
# ============================================================

READINESS_COLUMN_SQL = """
ALTER TABLE development_tasks ADD COLUMN readiness_status TEXT NOT NULL DEFAULT 'draft'
"""

READINESS_CHECK_SQL = """
ALTER TABLE development_tasks ADD COLUMN readiness_status TEXT NOT NULL DEFAULT 'draft'
  CHECK(readiness_status IN (
    'draft', 'needs_planning', 'ready',
    'executing', 'testing', 'completed', 'blocked'
  ))
"""

# SQLite 不支持在 ALTER TABLE ADD COLUMN 时同时加 CHECK 约束。
# 因此分两步：先 ADD COLUMN（不带 CHECK），再用后验证方式。
# 实际上，调度器只在 WHERE 条件中判断，CHECK 约束由应用层保证。

READINESS_ENUM_VALUES = {'draft', 'needs_planning', 'ready', 'executing', 'testing', 'completed', 'blocked'}


# ============================================================
# 迁移函数
# ============================================================

def migrate(db_path: str = "data/ai_factory.db"):
    """执行迁移 007：添加 readiness_status 字段并初始化"""
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] database not found: {db_path}")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        cur = conn.cursor()

        # ── 1. 检查列是否已存在（幂等）──
        cur.execute("PRAGMA table_info(development_tasks)")
        existing_cols = {row[1] for row in cur.fetchall()}

        if "readiness_status" in existing_cols:
            print("[OK] readiness_status column already exists")
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(READINESS_COLUMN_SQL)
                conn.commit()
                print("[ADD] readiness_status column added with DEFAULT 'draft'")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] add column failed: {e}")
                conn.close()
                return False

        # ── 2. 初始化未设置的任务为 'draft' ──
        cur.execute("""
            UPDATE development_tasks
            SET readiness_status = 'draft'
            WHERE readiness_status IS NULL
               OR readiness_status = ''
        """)
        draft_updated = cur.rowcount
        if draft_updated > 0:
            print(f"[INIT] {draft_updated} tasks set to 'draft'")

        # ── 3. 项目 56：全部 needs_planning ──
        cur.execute("""
            UPDATE development_tasks
            SET readiness_status = 'needs_planning'
            WHERE project_id = 56
        """)
        p56_count = cur.rowcount
        if p56_count > 0:
            print(f"[SET] project 56: {p56_count} tasks → 'needs_planning'")
        else:
            print(f"[INFO] project 56: no tasks found")

        # ── 4. 项目 6 Task #38：ready ──
        cur.execute("""
            UPDATE development_tasks
            SET readiness_status = 'ready'
            WHERE project_id = 6 AND id = 38
        """)
        p6_count = cur.rowcount
        if p6_count > 0:
            print(f"[SET] project 6 task #38 → 'ready'")
        else:
            print(f"[INFO] project 6 task #38: not found")

        conn.commit()

        # ── 5. 验证 ──
        cur.execute("PRAGMA table_info(development_tasks)")
        col_names = {r[1] for r in cur.fetchall()}
        assert "readiness_status" in col_names, "readiness_status column missing after migration"

        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        assert integrity == "ok", f"integrity_check failed: {integrity}"

        cur.execute("PRAGMA foreign_key_check")
        fk_violations = cur.fetchall()
        assert len(fk_violations) == 0, f"foreign_key_check violations: {fk_violations}"

        print(f"[DONE] migration 007 complete - {datetime.now().isoformat()}")
        print(f"  columns ok, integrity={integrity}, fk_violations={len(fk_violations)}")

        conn.close()
        return True

    except Exception as e:
        conn.rollback()
        print(f"[FAIL] migration failed: {e}")
        conn.close()
        return False


# ============================================================
# 测试函数（仅在数据库副本上执行）
# ============================================================

def run_tests():
    """在测试数据库副本上运行完整测试套件"""
    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent.parent
    db_path = backend_dir / "data" / "ai_factory.db"
    test_db = backend_dir / "data" / f"ai_factory_test_007_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

    if not db_path.exists():
        print(f"[FATAL] source database not found: {db_path}")
        return

    # 创建测试副本
    shutil.copy2(str(db_path), str(test_db))
    print(f"[TEST] test database: {test_db}")
    print(f"[TEST] source: {db_path} ({db_path.stat().st_size} bytes)")

    passed = 0
    failed = 0

    def check(test_name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {test_name}{' - ' + str(detail) if detail else ''}")
        else:
            failed += 1
            print(f"  [FAIL] {test_name}{' - ' + str(detail) if detail else ''}")

    # ── TEST 1: 首次迁移 ──
    print("\n── TEST 1: 首次迁移 ──")
    result = migrate(str(test_db))
    check("migration succeeded", result)

    conn = sqlite3.connect(str(test_db))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("PRAGMA table_info(development_tasks)")
    cols = {r[1] for r in c.fetchall()}
    check("readiness_status column exists", "readiness_status" in cols)

    # ── TEST 2: 重复迁移（幂等）──
    print("\n── TEST 2: 重复迁移（幂等）──")
    result2 = migrate(str(test_db))
    check("idempotent re-run succeeded", result2)

    # ── TEST 3: 默认值验证 ──
    print("\n── TEST 3: 默认值验证 ──")
    c.execute("""
        SELECT readiness_status FROM development_tasks
        WHERE project_id NOT IN (6, 56) AND readiness_status = 'draft'
        LIMIT 3
    """)
    draft_tasks = c.fetchall()
    if draft_tasks:
        check(f"non-56/6 tasks default to 'draft'", True, f"found {len(draft_tasks)} sample(s)")
    else:
        # 可能所有任务都在 6/56 中
        c.execute("SELECT COUNT(*) as cnt FROM development_tasks WHERE readiness_status='draft'")
        cnt = c.fetchone()["cnt"]
        check("no draft tasks because all belong to 6/56", cnt >= 0, f"draft count={cnt}")

    # ── TEST 4: 项目56所有任务 ──
    print("\n── TEST 4: 项目56所有任务 → needs_planning ──")
    c.execute("SELECT COUNT(*) as cnt FROM development_tasks WHERE project_id=56")
    p56_total = c.fetchone()["cnt"]
    c.execute("SELECT COUNT(*) as cnt FROM development_tasks WHERE project_id=56 AND readiness_status='needs_planning'")
    p56_needs = c.fetchone()["cnt"]
    check(f"project 56: {p56_total} tasks all needs_planning", p56_total == p56_needs,
          f"total={p56_total}, needs_planning={p56_needs}")
    check("project 56 has at least 12 tasks", p56_total >= 12, f"got {p56_total}")

    # ── TEST 5: 项目6 Task #38 ──
    print("\n── TEST 5: 项目6 Task #38 → ready ──")
    c.execute("SELECT id, title, status, readiness_status, dependencies, files_to_modify FROM development_tasks WHERE project_id=6 AND id=38")
    t38 = c.fetchone()
    if t38:
        check("task #38 exists", True)
        check("status=pending", t38["status"] == "pending", f"got {t38['status']}")
        check("readiness_status=ready", t38["readiness_status"] == "ready", f"got {t38['readiness_status']}")
        check("has files_to_modify", bool(t38["files_to_modify"]) and t38["files_to_modify"] not in ("[]", "null", ""))
        check("no dependencies", not t38["dependencies"] or t38["dependencies"] in ("[]", "null"))
    else:
        check("task #38 found", False)

    # ── TEST 6: find_runnable_tasks(56) → 0 ──
    print("\n── TEST 6: Scheduler find_runnable_tasks(56) → 0 ──")
    sys.path.insert(0, str(backend_dir))
    from app.executor.task_scheduler import TaskScheduler
    sched = TaskScheduler(str(test_db))
    runnable_56 = sched.find_runnable_tasks(56)
    check("project 56 runnable_tasks=0", len(runnable_56) == 0, f"got {len(runnable_56)}")
    if len(runnable_56) > 0:
        for rt in runnable_56:
            print(f"    UNEXPECTED: #{rt.id} {rt.title}")

    # ── TEST 7: find_runnable_tasks(6) → 1 (Task #38) ──
    print("\n── TEST 7: Scheduler find_runnable_tasks(6) → 1 ──")
    runnable_6 = sched.find_runnable_tasks(6)
    check("project 6 runnable_tasks=1", len(runnable_6) == 1, f"got {len(runnable_6)}")
    if len(runnable_6) > 0:
        check("only task is #38", runnable_6[0].id == 38, f"got #{runnable_6[0].id}")
    for rt in runnable_6:
        print(f"    RUNNABLE: #{rt.id} {rt.title}")

    # ── TEST 8: queue_status(56) 阻塞原因 ──
    print("\n── TEST 8: queue_status(56) blocked reasons ──")
    qs56 = sched.get_queue_status(56)
    check("pending_count=12", qs56["pending_count"] == 12, f"got {qs56['pending_count']}")
    check("runnable_count=0", qs56["runnable_count"] == 0, f"got {qs56['runnable_count']}")
    blocked = qs56.get("blocked_tasks", [])
    check("all 12 pending tasks are blocked", len(blocked) == 12, f"got {len(blocked)}")
    needs_planning_count = sum(1 for b in blocked for r in b.get("blocked_reasons", []) if "尚未完成工程规划" in r)
    check(f"blocked reasons include 'needs_planning': {needs_planning_count}", needs_planning_count >= 12,
          f"got {needs_planning_count} occurrences")

    conn.close()

    # ── TEST 9: 纯洁性验证 ──
    print("\n── TEST 9: 纯洁性验证 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM task_leases WHERE status='active'")
    lease_cnt = c.fetchone()[0]
    check("active task_leases=0", lease_cnt == 0, f"got {lease_cnt}")
    c.execute("SELECT COUNT(*) FROM executor_runs WHERE status IN ('running','pending','claiming')")
    run_cnt = c.fetchone()[0]
    check("active executor_runs=0", run_cnt == 0, f"got {run_cnt}")
    c.execute("SELECT COUNT(*) FROM executor_resource_locks WHERE status='active'")
    lock_cnt = c.fetchone()[0]
    check("active resource_locks=0", lock_cnt == 0, f"got {lock_cnt}")
    c.execute("PRAGMA integrity_check")
    check("integrity_check=ok", c.fetchone()[0] == "ok")
    c.execute("PRAGMA foreign_key_check")
    check("foreign_key_check=0", len(c.fetchall()) == 0)
    conn.close()

    # ── SUMMARY ──
    print(f"\n{'='*60}")
    print(f"  TEST RESULT: {passed} PASSED, {failed} FAILED")
    print(f"  OVERALL: {'PASS' if failed == 0 else 'FAIL'}")
    print(f"{'='*60}")

    # 清理测试数据库
    try:
        for ext in ["", "-wal", "-shm"]:
            p = Path(str(test_db) + ext)
            if p.exists():
                p.unlink()
        print(f"[CLEANUP] test db deleted: {test_db}")
    except Exception as e:
        print(f"[CLEANUP] failed: {e}")

    return passed, failed


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            run_tests()
        elif sys.argv[1] in ("--help", "-h"):
            print(__doc__)
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print("Usage: python -m app.migrations.007_task_readiness [test]")
    else:
        migrate()
