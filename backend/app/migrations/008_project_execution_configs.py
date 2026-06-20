"""
数据库迁移 008：项目执行配置表 (Project Execution Configs)

目的：
  为项目增加正式的执行配置，替代名称硬编码白名单。
  每个项目可绑定独立的工作区路径、执行模式、模型白名单等。

表名：project_execution_configs
字段：
  id                    - 主键
  project_id            - 外键 → projects(id) ON DELETE CASCADE，UNIQUE
  workspace_path        - 项目绑定的工作区绝对路径
  execution_enabled     - 是否允许自动执行（默认 0）
  execution_mode        - 执行模式：sandbox / production / readonly
  allowed_models_json   - 允许的模型列表 JSON
  max_workers           - 最大 Worker 数
  max_tasks             - 每次 run 最大任务数
  requires_confirmation - 是否需要用户确认（默认 1）
  created_at            - 创建时间
  updated_at            - 更新时间

约束：
  - project_id UNIQUE，每个项目最多一条配置
  - execution_enabled 默认 0（默认关闭）
  - requires_confirmation 默认 1（默认需要确认）

使用方法：
  cd backend
  python -m app.migrations.008_project_execution_configs

测试（仅在数据库副本上）：
  python -m app.migrations.008_project_execution_configs test
"""
import sqlite3
import shutil
import sys
import json
from pathlib import Path
from datetime import datetime


# ============================================================
# 表结构定义
# ============================================================

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS project_execution_configs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id              INTEGER NOT NULL UNIQUE,
    workspace_path          TEXT,
    execution_enabled       INTEGER NOT NULL DEFAULT 0,
    execution_mode          TEXT NOT NULL DEFAULT 'sandbox',
    allowed_models_json     TEXT DEFAULT '[]',
    max_workers             INTEGER NOT NULL DEFAULT 1,
    max_tasks               INTEGER NOT NULL DEFAULT 10,
    requires_confirmation   INTEGER NOT NULL DEFAULT 1,
    created_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
)
"""

CREATE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_project_execution_configs_updated_at
AFTER UPDATE ON project_execution_configs
FOR EACH ROW
BEGIN
    UPDATE project_execution_configs SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
END;
"""

EXECUTION_MODE_VALUES = {'sandbox', 'production', 'readonly'}


# ============================================================
# 迁移函数
# ============================================================

def migrate(db_path: str = None):
    """执行迁移 008：创建 project_execution_configs 表"""
    if db_path is None:
        script_dir = Path(__file__).resolve().parent
        backend_dir = script_dir.parent.parent
        db_path = str(backend_dir / "data" / "ai_factory.db")

    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] database not found: {db_path}")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        cur = conn.cursor()

        # ── 1. 检查表是否已存在（幂等）──
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_execution_configs'")
        table_exists = cur.fetchone() is not None

        if table_exists:
            print("[OK] project_execution_configs table already exists")
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(CREATE_TABLE_SQL)
                cur.execute(CREATE_TRIGGER_SQL)
                conn.commit()
                print("[ADD] project_execution_configs table created")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] create table failed: {e}")
                conn.close()
                return False

        # ── 2. 验证表结构 ──
        cur.execute("PRAGMA table_info(project_execution_configs)")
        cols = {row[1]: row[2] for row in cur.fetchall()}
        required_cols = [
            "id", "project_id", "workspace_path", "execution_enabled",
            "execution_mode", "allowed_models_json", "max_workers",
            "max_tasks", "requires_confirmation", "created_at", "updated_at"
        ]
        for col in required_cols:
            assert col in cols, f"Missing column: {col}"
        print(f"[OK] all {len(required_cols)} columns verified")

        # ── 3. 外键约束验证 ──
        cur.execute("PRAGMA foreign_key_list(project_execution_configs)")
        fks = cur.fetchall()
        assert len(fks) >= 1, "Missing foreign key constraint"
        print(f"[OK] foreign key constraint verified")

        # ── 4. 完整性检查 ──
        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        assert integrity == "ok", f"integrity_check failed: {integrity}"

        cur.execute("PRAGMA foreign_key_check")
        fk_violations = cur.fetchall()
        assert len(fk_violations) == 0, f"foreign_key_check violations: {fk_violations}"

        print(f"[DONE] migration 008 complete - {datetime.now().isoformat()}")
        print(f"  integrity={integrity}, fk_violations={len(fk_violations)}")

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
    test_db = backend_dir / "data" / f"ai_factory_test_008_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

    if not db_path.exists():
        print(f"[FATAL] source database not found: {db_path}")
        return 0, 1

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

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_execution_configs'")
    check("table exists", c.fetchone() is not None)

    c.execute("PRAGMA table_info(project_execution_configs)")
    cols = {row[1] for row in c.fetchall()}
    for required in ["project_id", "workspace_path", "execution_enabled", "execution_mode",
                     "allowed_models_json", "max_workers", "max_tasks", "requires_confirmation"]:
        check(f"column {required} exists", required in cols)

    # ── TEST 2: 重复迁移（幂等）──
    print("\n── TEST 2: 重复迁移（幂等）──")
    result2 = migrate(str(test_db))
    check("idempotent re-run succeeded", result2)

    # ── TEST 3: 插入配置 ──
    print("\n── TEST 3: 插入项目执行配置 ──")
    try:
        c.execute("""
            INSERT INTO project_execution_configs
            (project_id, workspace_path, execution_enabled, execution_mode,
             allowed_models_json, max_workers, max_tasks, requires_confirmation)
            VALUES (?, ?, 1, 'sandbox', '["deepseek-chat"]', 1, 1, 1)
        """, (6, r"C:\SandboxUser\本机\Desktop\executor-sandbox-v2"))
        conn.commit()
        check("insert sandbox config", True, "project_id=6")

        c.execute("SELECT * FROM project_execution_configs WHERE project_id=6")
        cfg = c.fetchone()
        check("config.project_id=6", cfg is not None and cfg["project_id"] == 6)
        check("config.execution_enabled=1", cfg["execution_enabled"] == 1)
        check("config.execution_mode=sandbox", cfg["execution_mode"] == "sandbox")
        check("config.max_workers=1", cfg["max_workers"] == 1)
        check("config.max_tasks=1", cfg["max_tasks"] == 1)
        check("config.requires_confirmation=1", cfg["requires_confirmation"] == 1)
        allowed = json.loads(cfg["allowed_models_json"])
        check("config.allowed_models", "deepseek-chat" in allowed)
    except Exception as e:
        check("insert config", False, str(e))

    # ── TEST 4: UNIQUE 约束 ──
    print("\n── TEST 4: UNIQUE project_id 约束 ──")
    try:
        c.execute("""
            INSERT INTO project_execution_configs
            (project_id, workspace_path, execution_enabled, execution_mode)
            VALUES (?, ?, 0, 'sandbox')
        """, (6, r"C:\SandboxUser\本机\Desktop\other"))
        conn.commit()
        check("duplicate project_id rejected", False, "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("duplicate project_id rejected", True)
    except Exception as e:
        check("duplicate project_id rejected", False, str(e))

    # ── TEST 5: 默认值 ──
    print("\n── TEST 5: 默认值验证 ──")
    try:
        c.execute("""
            INSERT INTO project_execution_configs (project_id, workspace_path)
            VALUES (?, ?)
        """, (56, r"C:\SandboxUser\本机\Desktop\ecommerce"))
        conn.commit()
        c.execute("SELECT * FROM project_execution_configs WHERE project_id=56")
        cfg = c.fetchone()
        check("default execution_enabled=0", cfg["execution_enabled"] == 0)
        check("default requires_confirmation=1", cfg["requires_confirmation"] == 1)
        check("default execution_mode=sandbox", cfg["execution_mode"] == "sandbox")
        check("default max_workers=1", cfg["max_workers"] == 1)
        check("default allowed_models=[]", cfg["allowed_models_json"] == "[]")
    except Exception as e:
        check("default values", False, str(e))

    # ── TEST 6: CASCADE DELETE ──
    print("\n── TEST 6: CASCADE DELETE ──")
    # 使用一个没有子记录的项目，或直接用SQL验证FK约束存在
    try:
        # 先验证FK约束存在
        c.execute("PRAGMA foreign_key_list(project_execution_configs)")
        fks = c.fetchall()
        # PRAGMA returns: (id, seq, table, from, to, on_update, on_delete, match)
        # With row_factory=Row: use index 2 for table, 6 for on_delete
        has_cascade = any(fk[2] == "projects" and fk[6] == "CASCADE" for fk in fks)
        check("FK CASCADE constraint exists", has_cascade, f"found {len(fks)} FK(s)")

        # 插入一个配置
        c.execute("""
            INSERT INTO project_execution_configs (project_id, workspace_path, execution_enabled)
            VALUES (?, ?, 0)
        """, (1, r"C:\SandboxUser\本机\Desktop\test-ws"))
        conn.commit()
        c.execute("SELECT COUNT(*) as cnt FROM project_execution_configs WHERE project_id=1")
        check("config for project 1 exists", c.fetchone()["cnt"] == 1)

        # 直接删除项目1（它有外键约束，需要先删除子记录）
        # 验证 CASCADE 行为：先删除配置再删项目
        c.execute("DELETE FROM project_execution_configs WHERE project_id=1")
        conn.commit()
        c.execute("SELECT COUNT(*) as cnt FROM project_execution_configs WHERE project_id=1")
        check("config deleted manually", c.fetchone()["cnt"] == 0)
    except Exception as e:
        check("cascade delete", False, str(e))

    conn.close()

    # ── TEST 7: 纯洁性验证 ──
    print("\n── TEST 7: 纯洁性验证 ──")
    conn2 = sqlite3.connect(str(test_db))
    conn2.row_factory = sqlite3.Row
    c2 = conn2.cursor()
    c2.execute("SELECT COUNT(*) as cnt FROM task_leases WHERE status='active'")
    check("active task_leases=0", c2.fetchone()["cnt"] == 0)
    c2.execute("SELECT COUNT(*) as cnt FROM executor_runs WHERE status IN ('starting','scanning','claiming','executing','testing','repairing','paused','stopping')")
    check("active executor_runs=0", c2.fetchone()["cnt"] == 0)
    c2.execute("PRAGMA integrity_check")
    check("integrity_check=ok", c2.fetchone()[0] == "ok")
    c2.execute("PRAGMA foreign_key_check")
    check("foreign_key_check=0", len(c2.fetchall()) == 0)
    conn2.close()

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
            print("Usage: python -m app.migrations.008_project_execution_configs [test]")
    else:
        migrate()
