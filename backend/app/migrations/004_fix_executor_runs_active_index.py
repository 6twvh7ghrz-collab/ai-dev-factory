"""
数据库迁移 004：修正 executor_runs 部分唯一索引

目的：
  加固同一 project 最多一条活跃 run 的约束。

活跃状态定义（8 个）：
  starting, scanning, claiming, executing, testing, repairing, paused, stopping

非活跃状态（不受唯一索引限制，可与活跃 run 共存）：
  idle（默认初始状态，未开始运行）
  completed（正常结束）
  blocked（无可用任务，可恢复）
  failed（异常终止）

状态机说明：
  idle → starting → scanning → claiming → executing → testing → repairing
                                                          ↓
                                          paused ←→ (resume)
                                                          ↓
                                          completed / blocked / failed
                                                          ↓
                                                        stopping

  - idle: 新创建 run 的初始默认值，不表示活跃循环
  - LoopController 必须将新 run 立即从 idle 转为 starting
  - 不得将 idle 作为规避活跃唯一约束的状态

使用方法：
  cd backend
  python -m app.migrations.004_fix_executor_runs_active_index

测试（仅在数据库副本上）：
  python -m app.migrations.004_fix_executor_runs_active_index test
"""
import sqlite3
import uuid
import shutil
import threading
import time
import random
from pathlib import Path
from datetime import datetime


# ============================================================
# 活跃状态定义
# ============================================================
ACTIVE_STATUSES = [
    'starting', 'scanning', 'claiming', 'executing',
    'testing', 'repairing', 'paused', 'stopping',
]

TERMINAL_STATUSES = ['completed', 'blocked', 'failed']
NON_ACTIVE_STATUSES = ['idle'] + TERMINAL_STATUSES

ACTIVE_STATUS_LIST_SQL = ",\n    ".join(f"'{s}'" for s in ACTIVE_STATUSES)

# ============================================================
# 修正后的部分唯一索引 DDL
# ============================================================
CREATE_ACTIVE_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS uq_executor_runs_active_project
ON executor_runs(project_id)
WHERE project_id IS NOT NULL
AND status IN (
    {ACTIVE_STATUS_LIST_SQL}
)
"""

# ============================================================
# 迁移函数
# ============================================================
def migrate(db_path: str = "data/ai_factory.db"):
    """执行迁移 004"""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        conn.execute("BEGIN IMMEDIATE")

        # 检查 executor_runs 表是否存在
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='executor_runs'")
        if not c.fetchone():
            raise RuntimeError("executor_runs 表不存在，请先执行迁移 003")

        # 检查是否存在同一 project 的多条活跃 run（冲突检测）
        c.execute("""
            SELECT project_id, COUNT(*) as cnt
            FROM executor_runs
            WHERE project_id IS NOT NULL
              AND status IN ({})
            GROUP BY project_id
            HAVING COUNT(*) > 1
        """.format(", ".join(f"'{s}'" for s in ACTIVE_STATUSES)))
        conflicts = c.fetchall()
        if conflicts:
            conflict_detail = []
            for pid, cnt in conflicts:
                c.execute(
                    "SELECT run_id, status, worker_id FROM executor_runs WHERE project_id=? AND status IN ({})".format(
                        ", ".join(f"'{s}'" for s in ACTIVE_STATUSES)
                    ),
                    (pid,),
                )
                runs = c.fetchall()
                conflict_detail.append(f"project_id={pid}, count={cnt}: {runs}")
            conn.rollback()
            raise RuntimeError(
                "检测到同一项目存在多条活跃 run，禁止迁移。\n"
                "冲突详情：\n" + "\n".join(conflict_detail) +
                "\n请人工处理后重试。"
            )

        # 删除旧索引
        c.execute("DROP INDEX IF EXISTS uq_executor_runs_active_project")

        # 创建新索引
        c.execute(CREATE_ACTIVE_INDEX_SQL)

        conn.commit()
        print("[OK] uq_executor_runs_active_project rebuilt with 8 active statuses")
        print(f"     active: {ACTIVE_STATUSES}")
        print(f"     non-active (allowed to coexist): {NON_ACTIVE_STATUSES}")

        # 验证
        c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_executor_runs_active_project'")
        ddl = c.fetchone()[0]
        for s in ACTIVE_STATUSES:
            if f"'{s}'" not in ddl:
                print(f"[WARN] status '{s}' not found in index DDL")
        print(f"[DONE] 迁移 004 完成 - {datetime.now().isoformat()}")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rollback(db_path: str = "data/ai_factory.db"):
    """回滚迁移 004（恢复到迁移前的索引状态）"""
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        conn.execute("BEGIN IMMEDIATE")
        c = conn.cursor()

        # 重建索引为迁移 003 的原始定义（8 个活跃状态，不含 idle）
        c.execute("DROP INDEX IF EXISTS uq_executor_runs_active_project")
        c.execute(CREATE_ACTIVE_INDEX_SQL)

        conn.commit()
        print("[OK] 回滚完成 - 索引恢复为 8 活跃状态定义")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# 测试（在数据库副本上运行）
# ============================================================
def test(db_path: str = "data/ai_factory.db"):
    """在数据库副本上运行迁移 004 测试"""
    src = Path(db_path)
    if not src.exists():
        print(f"[FATAL] 数据库不存在: {src}")
        return

    test_db = src.parent / f"_test_004_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(str(src), str(test_db))
    print(f"[INFO] 测试数据库副本: {test_db}")

    passed = 0
    failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name} | {detail}")

    try:
        # === Test 1: 首次迁移 ===
        print("\n--- Test 1: 首次迁移 ---")
        try:
            migrate(str(test_db))
            check("首次迁移成功", True)
        except Exception as e:
            check("首次迁移成功", False, str(e))

        # 验证索引存在且包含全部 8 个活跃状态
        conn = sqlite3.connect(str(test_db))
        c = conn.cursor()
        c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_executor_runs_active_project'")
        ddl = c.fetchone()[0]
        for s in ACTIVE_STATUSES:
            check(f"索引包含 '{s}'", f"'{s}'" in ddl, ddl)
        conn.close()

        # === Test 2: 重复迁移（幂等） ===
        print("\n--- Test 2: 重复迁移（幂等） ---")
        try:
            migrate(str(test_db))
            check("重复迁移幂等", True)
        except Exception as e:
            check("重复迁移幂等", False, str(e))

        # === Test 3: 失败注入回滚 ===
        print("\n--- Test 3: 失败注入回滚 ---")
        shutil.copy2(str(src), str(test_db) + ".rollback_test")
        rollback_test_db = Path(str(test_db) + ".rollback_test")

        conn = sqlite3.connect(str(rollback_test_db))
        conn.execute("PRAGMA foreign_keys = ON")
        c = conn.cursor()
        # 获取当前索引 DDL
        c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_executor_runs_active_project'")
        old_ddl = c.fetchone()
        old_ddl_text = old_ddl[0] if old_ddl else "(none)"
        conn.close()

        # 模拟失败：手动 DROP 索引后，尝试执行迁移中一个无效 SQL 来触发 ROLLBACK
        conn = sqlite3.connect(str(rollback_test_db))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            c = conn.cursor()
            c.execute("DROP INDEX IF EXISTS uq_executor_runs_active_project")
            # 故意执行无效 SQL 触发回滚
            c.execute("THIS_IS_NOT_VALID_SQL")
            conn.commit()
        except sqlite3.OperationalError:
            conn.rollback()
            check("失败注入触发回滚", True)
        except Exception as e:
            conn.rollback()
            check("失败注入触发回滚", True, str(e))
        finally:
            conn.close()

        # 验证回滚后索引是否恢复（因为是 BEGIN IMMEDIATE + ROLLBACK，索引 DROP 也被回滚）
        conn = sqlite3.connect(str(rollback_test_db))
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_executor_runs_active_project'")
        restored = c.fetchone()
        check("回滚后索引恢复", restored is not None)
        conn.close()

        # 清理 rollback test db
        rollback_test_db.unlink(missing_ok=True)

        # === Test 4: 旧索引已被替换 ===
        print("\n--- Test 4: 索引 DDL 验证 ---")
        conn = sqlite3.connect(str(test_db))
        c = conn.cursor()
        c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_executor_runs_active_project'")
        ddl = c.fetchone()[0]

        # idle 不应在索引中
        check("idle 不在索引中", "'idle'" not in ddl)

        # 所有 8 个活跃状态都在索引中
        for s in ACTIVE_STATUSES:
            check(f"活跃状态 '{s}' 在索引中", f"'{s}'" in ddl)

        # WHERE 条件完整
        check("WHERE project_id IS NOT NULL", "PROJECT_ID IS NOT NULL" in ddl.upper())
        check("WHERE status IN (...)", "status IN (" in ddl)
        conn.close()

        # === Test 5: integrity / foreign_key ===
        print("\n--- Test 5: 完整性检查 ---")
        conn = sqlite3.connect(str(test_db))
        c = conn.cursor()
        c.execute("PRAGMA integrity_check")
        check("integrity_check", c.fetchone()[0] == "ok")
        c.execute("PRAGMA foreign_key_check")
        fk_count = len(c.fetchall())
        check("foreign_key_check", fk_count == 0, f"count={fk_count}")
        conn.close()

        # === Test 6: 8x8 活跃状态组合测试 ===
        print("\n--- Test 6: 活跃状态组合测试 ---")
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = ON")
        c = conn.cursor()

        c.execute("SELECT id FROM projects LIMIT 1")
        pid = c.fetchone()[0]

        for status_a in ACTIVE_STATUSES:
            for status_b in ACTIVE_STATUSES:
                # 清理
                c.execute("DELETE FROM executor_runs WHERE run_id LIKE 'combo-%'")
                conn.commit()

                # 插入第一条活跃 run
                rid_a = f"combo-a-{status_a}"
                c.execute(
                    "INSERT INTO executor_runs (run_id, project_id, worker_id, status, mode) VALUES (?, ?, 'w-a', ?, 'auto_until_blocked')",
                    (rid_a, pid, status_a),
                )
                conn.commit()

                # 尝试插入第二条活跃 run（应被拒绝）
                rid_b = f"combo-b-{status_b}"
                try:
                    c.execute(
                        "INSERT INTO executor_runs (run_id, project_id, worker_id, status, mode) VALUES (?, ?, 'w-b', ?, 'auto_until_blocked')",
                        (rid_b, pid, status_b),
                    )
                    conn.commit()
                    check(f"{status_a} + {status_b} 被拒绝", False, "未拒绝")
                except sqlite3.IntegrityError:
                    check(f"{status_a} + {status_b} 被拒绝", True)
                    conn.rollback()

                # 清理
                c.execute("DELETE FROM executor_runs WHERE run_id LIKE 'combo-%'")
                conn.commit()

        conn.close()

        # === Test 7: 终态共存测试 ===
        print("\n--- Test 7: 终态共存测试 ---")
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = ON")
        c = conn.cursor()

        c.execute("SELECT id FROM projects LIMIT 1")
        pid = c.fetchone()[0]

        # 插入一条活跃 run
        c.execute(
            "INSERT INTO executor_runs (run_id, project_id, worker_id, status, mode) VALUES ('term-active', ?, 'w-active', 'executing', 'auto_until_blocked')",
            (pid,),
        )
        conn.commit()

        # 终态（completed, blocked, failed）应能与活跃 run 共存
        for term_status in TERMINAL_STATUSES:
            rid = f"term-{term_status}"
            try:
                c.execute(
                    "INSERT INTO executor_runs (run_id, project_id, worker_id, status, mode) VALUES (?, ?, 'w-term', ?, 'auto_until_blocked')",
                    (rid, pid, term_status),
                )
                conn.commit()
                check(f"终态 {term_status} 与 executing 共存", True)
            except sqlite3.IntegrityError as e:
                check(f"终态 {term_status} 与 executing 共存", False, str(e))

        # idle 也应能共存
        try:
            c.execute(
                "INSERT INTO executor_runs (run_id, project_id, worker_id, status, mode) VALUES ('term-idle', ?, 'w-idle', 'idle', 'auto_until_blocked')",
                (pid,),
            )
            conn.commit()
            check("idle 与 executing 共存", True)
        except sqlite3.IntegrityError as e:
            check("idle 与 executing 共存", False, str(e))

        # 清理
        c.execute("DELETE FROM executor_runs WHERE run_id LIKE 'term-%' OR run_id LIKE 'combo-%'")
        conn.commit()
        conn.close()

        # === Test 8: 100轮并发启动测试 ===
        print("\n--- Test 8: 100轮并发启动测试 ---")
        double_success_rounds = 0  # 同一轮两个线程都成功的次数
        db_locked = 0
        lock = threading.Lock()

        def concurrent_insert(round_num, success_list):
            conn2 = sqlite3.connect(str(test_db), timeout=3)
            conn2.execute("PRAGMA foreign_keys = ON")
            c2 = conn2.cursor()

            # 随机选择活跃状态
            status_choice = random.choice(ACTIVE_STATUSES)
            rid = f"conc-{round_num}-{threading.get_ident()}-{uuid.uuid4().hex[:4]}"

            try:
                c2.execute(
                    "INSERT INTO executor_runs (run_id, project_id, worker_id, status, mode) VALUES (?, ?, ?, ?, 'auto_until_blocked')",
                    (rid, pid, f"w-{threading.get_ident()}", status_choice),
                )
                conn2.commit()
                success_list.append(True)
            except sqlite3.IntegrityError:
                conn2.rollback()
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower():
                    with lock:
                        db_locked += 1
                conn2.rollback()
            finally:
                conn2.close()

        for round_num in range(100):
            # 先清理上轮残留
            conn_clean = sqlite3.connect(str(test_db))
            conn_clean.execute("PRAGMA foreign_keys = ON")
            cc = conn_clean.cursor()
            cc.execute("DELETE FROM executor_runs WHERE run_id LIKE 'conc-%'")
            conn_clean.commit()
            conn_clean.close()

            round_results = []
            t1 = threading.Thread(target=concurrent_insert, args=(round_num, round_results))
            t2 = threading.Thread(target=concurrent_insert, args=(round_num, round_results))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # 检查本轮是否有两个线程都成功（双成功）
            if len(round_results) >= 2:
                double_success_rounds += 1
                print(f"  [FAIL] Round {round_num}: double success detected!")

            # 验证每轮只有一条活跃 run
            conn_verify = sqlite3.connect(str(test_db))
            cv = conn_verify.cursor()
            cv.execute("SELECT COUNT(*) FROM executor_runs WHERE run_id LIKE 'conc-%'")
            cnt = cv.fetchone()[0]
            conn_verify.close()
            if cnt != 1:
                print(f"  [WARN] Round {round_num}: {cnt} active runs created (expected 1)")

        check("100轮并发 double_success=0", double_success_rounds == 0, f"double_success_rounds={double_success_rounds}")
        check("100轮并发 db_locked=0", db_locked == 0, f"db_locked={db_locked}")

        # 最终清理
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = ON")
        c = conn.cursor()
        c.execute("DELETE FROM executor_runs WHERE run_id LIKE 'conc-%' OR run_id LIKE 'combo-%' OR run_id LIKE 'term-%'")
        conn.commit()

        # 最终完整性
        c.execute("PRAGMA integrity_check")
        check("最终 integrity_check", c.fetchone()[0] == "ok")
        c.execute("PRAGMA foreign_key_check")
        fk_count = len(c.fetchall())
        check("最终 foreign_key_check", fk_count == 0, f"count={fk_count}")
        conn.close()

    finally:
        # 清理测试数据库
        try:
            test_db.unlink(missing_ok=True)
        except Exception:
            pass

    print(f"\n{'='*50}")
    print(f"TEST RESULT: {passed} PASSED, {failed} FAILED")
    print(f"OVERALL: {'PASS' if failed == 0 else 'FAIL'}")

    return failed == 0


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "rollback":
        rollback()
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        test()
    else:
        migrate()
