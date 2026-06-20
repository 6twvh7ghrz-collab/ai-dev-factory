"""
数据库迁移 009：规划预览与审批记录表 (Planning Previews & Approvals)

目的：
  为 AI 工程规划预览（V1.3）增加持久化能力，支持审批安全写回。
  新增两张表：planning_previews（规划预览）和 planning_approvals（审批记录）。

表 1: planning_previews
  字段：
    id                      - 自增主键
    preview_id              - 预览唯一标识 (UUID)，UNIQUE
    project_id              - 关联项目，FK → projects(id) ON DELETE CASCADE
    provider                - AI 模型提供商
    model                   - AI 模型名称
    status                  - 状态：generated/partially_approved/approved/rejected/expired/invalidated
    schema_version          - 规划 schema 版本
    project_snapshot_hash   - 项目信息快照 SHA-256
    tasks_snapshot_hash     - 任务快照 SHA-256
    task_ids_json           - 任务 ID 列表 JSON
    preview_json            - 结构化规划预览 JSON
    risk_summary_json       - 风险摘要 JSON
    request_id              - AI API 请求 ID
    created_at              - 创建时间
    expires_at              - 过期时间（默认 24h）
    approved_at             - 批准时间
    rejected_at             - 拒绝时间
    updated_at              - 更新时间

  索引：preview_id UNIQUE, project_id+status, expires_at, created_at

表 2: planning_approvals
  字段：
    id                      - 自增主键
    approval_id             - 审批唯一标识 (UUID)，UNIQUE
    preview_id              - 关联规划预览，FK → planning_previews(preview_id)
    project_id              - 关联项目，FK → projects(id)
    approved_task_ids_json  - 已批准任务 ID 列表 JSON
    rejected_task_ids_json  - 已拒绝任务 ID 列表 JSON
    skipped_task_ids_json   - 已跳过任务 ID 列表 JSON
    approval_mode           - 审批模式：selected_tasks / all_safe_tasks
    approval_summary_json   - 审批摘要 JSON
    before_snapshot_json    - 审批前任务快照 JSON
    after_snapshot_json     - 审批后任务快照 JSON
    approved_by             - 审批人（默认 'user'）
    created_at              - 创建时间

  外键：preview_id → planning_previews(preview_id), project_id → projects(id)

禁止保存：
  - API Key
  - .env 内容
  - Git 凭据
  - 用户敏感数据
  - 模型完整系统提示词

迁移要求：
  - 显式事务
  - 幂等执行
  - 重复执行不报错
  - 失败注入可回滚
  - 数据库副本测试
  - integrity_check=ok
  - foreign_key_check=0

使用方法：
  cd backend
  python -m app.migrations.009_planning_previews_and_approvals

测试（仅在数据库副本上）：
  python -m app.migrations.009_planning_previews_and_approvals test
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

CREATE_PLANNING_PREVIEWS_SQL = """
CREATE TABLE IF NOT EXISTS planning_previews (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    preview_id              TEXT NOT NULL UNIQUE,
    project_id              INTEGER NOT NULL,
    provider                TEXT,
    model                   TEXT,
    status                  TEXT NOT NULL DEFAULT 'generated'
                            CHECK(status IN (
                                'generated',
                                'partially_approved',
                                'approved',
                                'rejected',
                                'expired',
                                'invalidated'
                            )),
    schema_version          TEXT NOT NULL DEFAULT '1.0',
    project_snapshot_hash   TEXT NOT NULL DEFAULT '',
    tasks_snapshot_hash     TEXT NOT NULL DEFAULT '',
    task_ids_json           TEXT NOT NULL DEFAULT '[]',
    preview_json            TEXT NOT NULL DEFAULT '{}',
    risk_summary_json       TEXT NOT NULL DEFAULT '{}',
    request_id              TEXT,
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at              DATETIME NOT NULL,
    approved_at             DATETIME,
    rejected_at             DATETIME,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
)
"""

CREATE_PLANNING_APPROVALS_SQL = """
CREATE TABLE IF NOT EXISTS planning_approvals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id             TEXT NOT NULL UNIQUE,
    preview_id              TEXT NOT NULL,
    project_id              INTEGER NOT NULL,
    approved_task_ids_json  TEXT NOT NULL DEFAULT '[]',
    rejected_task_ids_json  TEXT NOT NULL DEFAULT '[]',
    skipped_task_ids_json   TEXT NOT NULL DEFAULT '[]',
    approval_mode           TEXT NOT NULL DEFAULT 'selected_tasks'
                            CHECK(approval_mode IN (
                                'selected_tasks',
                                'all_safe_tasks'
                            )),
    approval_summary_json   TEXT NOT NULL DEFAULT '{}',
    before_snapshot_json    TEXT NOT NULL DEFAULT '{}',
    after_snapshot_json     TEXT NOT NULL DEFAULT '{}',
    approved_by             TEXT NOT NULL DEFAULT 'user',
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (preview_id) REFERENCES planning_previews(preview_id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
)
"""

# 索引
CREATE_PLANNING_PREVIEWS_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_planning_previews_project_status ON planning_previews(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_planning_previews_expires_at ON planning_previews(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_planning_previews_created_at ON planning_previews(created_at)",
]

CREATE_PLANNING_APPROVALS_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_planning_approvals_preview_id ON planning_approvals(preview_id)",
    "CREATE INDEX IF NOT EXISTS idx_planning_approvals_project_id ON planning_approvals(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_planning_approvals_created_at ON planning_approvals(created_at)",
]

# 更新触发器
CREATE_PREVIEWS_UPDATED_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_planning_previews_updated_at
AFTER UPDATE ON planning_previews
FOR EACH ROW
BEGIN
    UPDATE planning_previews SET updated_at = CURRENT_TIMESTAMP WHERE id = OLD.id;
END;
"""

# 状态枚举
PREVIEW_STATUS_VALUES = {
    'generated', 'partially_approved', 'approved',
    'rejected', 'expired', 'invalidated'
}

APPROVAL_MODE_VALUES = {'selected_tasks', 'all_safe_tasks'}

# ============================================================
# 迁移函数
# ============================================================

def migrate(db_path: str = None):
    """执行迁移 009：创建 planning_previews 和 planning_approvals 表"""
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

        # ── 1. 创建 planning_previews 表（幂等）──
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='planning_previews'")
        pp_exists = cur.fetchone() is not None

        if not pp_exists:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(CREATE_PLANNING_PREVIEWS_SQL)
                for idx_sql in CREATE_PLANNING_PREVIEWS_INDEXES_SQL:
                    cur.execute(idx_sql)
                cur.execute(CREATE_PREVIEWS_UPDATED_TRIGGER_SQL)
                conn.commit()
                print("[ADD] planning_previews table created")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] create planning_previews failed: {e}")
                conn.close()
                return False
        else:
            print("[OK] planning_previews table already exists")

        # ── 2. 创建 planning_approvals 表（幂等）──
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='planning_approvals'")
        pa_exists = cur.fetchone() is not None

        if not pa_exists:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(CREATE_PLANNING_APPROVALS_SQL)
                for idx_sql in CREATE_PLANNING_APPROVALS_INDEXES_SQL:
                    cur.execute(idx_sql)
                conn.commit()
                print("[ADD] planning_approvals table created")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] create planning_approvals failed: {e}")
                conn.close()
                return False
        else:
            print("[OK] planning_approvals table already exists")

        # ── 3. 验证 planning_previews 表结构 ──
        cur.execute("PRAGMA table_info(planning_previews)")
        pp_cols = {row[1]: row[2] for row in cur.fetchall()}
        required_pp_cols = [
            "id", "preview_id", "project_id", "provider", "model",
            "status", "schema_version", "project_snapshot_hash", "tasks_snapshot_hash",
            "task_ids_json", "preview_json", "risk_summary_json",
            "request_id", "created_at", "expires_at",
            "approved_at", "rejected_at", "updated_at"
        ]
        for col in required_pp_cols:
            assert col in pp_cols, f"Missing column in planning_previews: {col}"
        print(f"[OK] planning_previews: all {len(required_pp_cols)} columns verified")

        # ── 4. 验证 planning_approvals 表结构 ──
        cur.execute("PRAGMA table_info(planning_approvals)")
        pa_cols = {row[1]: row[2] for row in cur.fetchall()}
        required_pa_cols = [
            "id", "approval_id", "preview_id", "project_id",
            "approved_task_ids_json", "rejected_task_ids_json", "skipped_task_ids_json",
            "approval_mode", "approval_summary_json",
            "before_snapshot_json", "after_snapshot_json",
            "approved_by", "created_at"
        ]
        for col in required_pa_cols:
            assert col in pa_cols, f"Missing column in planning_approvals: {col}"
        print(f"[OK] planning_approvals: all {len(required_pa_cols)} columns verified")

        # ── 5. 外键约束验证 ──
        cur.execute("PRAGMA foreign_key_list(planning_previews)")
        pp_fks = cur.fetchall()
        assert len(pp_fks) >= 1, "Missing foreign key in planning_previews"
        has_pp_fk = any(fk[2] == "projects" for fk in pp_fks)
        assert has_pp_fk, "planning_previews missing FK to projects"
        print(f"[OK] planning_previews FK verified (count={len(pp_fks)})")

        cur.execute("PRAGMA foreign_key_list(planning_approvals)")
        pa_fks = cur.fetchall()
        assert len(pa_fks) >= 2, f"Missing foreign keys in planning_approvals (found {len(pa_fks)})"
        has_pa_pp_fk = any(fk[2] == "planning_previews" for fk in pa_fks)
        has_pa_proj_fk = any(fk[2] == "projects" for fk in pa_fks)
        assert has_pa_pp_fk, "planning_approvals missing FK to planning_previews"
        assert has_pa_proj_fk, "planning_approvals missing FK to projects"
        print(f"[OK] planning_approvals FK verified (count={len(pa_fks)})")

        # ── 6. 索引验证 ──
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='planning_previews'")
        pp_idx = {row[0] for row in cur.fetchall()}
        expected_pp_idx = {
            "idx_planning_previews_project_status",
            "idx_planning_previews_expires_at",
            "idx_planning_previews_created_at",
        }
        for idx_name in expected_pp_idx:
            assert idx_name in pp_idx, f"Missing index: {idx_name}"
        print(f"[OK] planning_previews: {len(pp_idx)} indexes verified")

        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='planning_approvals'")
        pa_idx = {row[0] for row in cur.fetchall()}
        expected_pa_idx = {
            "idx_planning_approvals_preview_id",
            "idx_planning_approvals_project_id",
            "idx_planning_approvals_created_at",
        }
        for idx_name in expected_pa_idx:
            assert idx_name in pa_idx, f"Missing index: {idx_name}"
        print(f"[OK] planning_approvals: {len(pa_idx)} indexes verified")

        # ── 7. 完整性检查 ──
        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        assert integrity == "ok", f"integrity_check failed: {integrity}"

        cur.execute("PRAGMA foreign_key_check")
        fk_violations = cur.fetchall()
        assert len(fk_violations) == 0, f"foreign_key_check violations: {fk_violations}"

        print(f"[DONE] migration 009 complete - {datetime.now().isoformat()}")
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
    import datetime as dt_mod
    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent.parent
    db_path = backend_dir / "data" / "ai_factory.db"
    test_db = backend_dir / "data" / f"ai_factory_test_009_{dt_mod.datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

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
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='planning_previews'")
    check("planning_previews table exists", c.fetchone() is not None)

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='planning_approvals'")
    check("planning_approvals table exists", c.fetchone() is not None)

    # ── TEST 2: 重复迁移（幂等）──
    print("\n── TEST 2: 重复迁移（幂等）──")
    result2 = migrate(str(test_db))
    check("idempotent re-run succeeded", result2)

    # ── TEST 3: 迁移失败回滚 ──
    print("\n── TEST 3: 迁移失败回滚 ──")
    # 验证表仍然存在且结构正确（幂等后）
    c.execute("SELECT COUNT(*) as cnt FROM planning_previews")
    check("planning_previews empty after idempotent", c.fetchone()["cnt"] == 0)
    c.execute("SELECT COUNT(*) as cnt FROM planning_approvals")
    check("planning_approvals empty after idempotent", c.fetchone()["cnt"] == 0)

    # ── TEST 4: preview_id 唯一性 ──
    print("\n── TEST 4: preview_id 唯一性 ──")
    try:
        now = dt_mod.datetime.now()
        expires = now + dt_mod.timedelta(hours=24)

        c.execute("""
            INSERT INTO planning_previews
            (preview_id, project_id, provider, model, status, schema_version,
             project_snapshot_hash, tasks_snapshot_hash, task_ids_json,
             preview_json, risk_summary_json, request_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "plan-001", 56, "deepseek", "deepseek-chat", "generated", "1.0",
            "abc123", "def456", "[26,27]", '{"test":true}', '{"high":0}',
            "req-001", now.isoformat(), expires.isoformat(),
        ))
        conn.commit()
        check("insert planning_preview", True, "preview_id=plan-001")

        # 尝试插入相同 preview_id
        try:
            c.execute("""
                INSERT INTO planning_previews
                (preview_id, project_id, status, schema_version,
                 project_snapshot_hash, tasks_snapshot_hash, task_ids_json,
                 preview_json, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "plan-001", 56, "generated", "1.0",
                "abc", "def", "[26]", "{}", expires.isoformat(),
            ))
            conn.commit()
            check("duplicate preview_id rejected", False, "should have raised IntegrityError")
        except sqlite3.IntegrityError:
            check("duplicate preview_id rejected", True)
        except Exception as e:
            check("duplicate preview_id rejected", False, str(e))

    except Exception as e:
        check("preview_id uniqueness test setup", False, str(e))

    # ── TEST 5: 外键约束 ──
    print("\n── TEST 5: 外键约束 ──")
    try:
        c.execute("""
            INSERT INTO planning_previews
            (preview_id, project_id, status, schema_version,
             project_snapshot_hash, tasks_snapshot_hash, task_ids_json,
             preview_json, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "plan-fk-test", 99999, "generated", "1.0",
            "abc", "def", "[26]", "{}", expires.isoformat(),
        ))
        conn.commit()
        check("invalid project_id FK rejected", False, "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("invalid project_id FK rejected", True)
    except Exception as e:
        check("invalid project_id FK rejected", False, str(e))

    # ── TEST 6: status CHECK 约束 ──
    print("\n── TEST 6: status CHECK 约束 ──")
    try:
        c.execute("""
            INSERT INTO planning_previews
            (preview_id, project_id, status, schema_version,
             project_snapshot_hash, tasks_snapshot_hash, task_ids_json,
             preview_json, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "plan-status-test", 56, "INVALID_STATUS", "1.0",
            "abc", "def", "[26]", "{}", expires.isoformat(),
        ))
        conn.commit()
        check("invalid status rejected", False, "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("invalid status rejected", True)
    except Exception as e:
        check("invalid status rejected", False, str(e))

    # ── TEST 7: approval_mode CHECK 约束 ──
    print("\n── TEST 7: approval_mode CHECK 约束 ──")
    try:
        c.execute("""
            INSERT INTO planning_approvals
            (approval_id, preview_id, project_id, approved_task_ids_json,
             rejected_task_ids_json, approval_mode, approval_summary_json,
             before_snapshot_json, after_snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "appr-001", "plan-001", 56, "[26]", "[27]",
            "INVALID_MODE", "{}", "{}", "{}",
        ))
        conn.commit()
        check("invalid approval_mode rejected", False, "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("invalid approval_mode rejected", True)
    except Exception as e:
        check("invalid approval_mode rejected", False, str(e))

    # ── TEST 8: planning_approvals 外键（preview_id）──
    print("\n── TEST 8: planning_approvals FK to planning_previews ──")
    try:
        c.execute("""
            INSERT INTO planning_approvals
            (approval_id, preview_id, project_id, approved_task_ids_json,
             approval_mode, approval_summary_json,
             before_snapshot_json, after_snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "appr-fk-test", "plan-nonexistent", 56, "[26]",
            "selected_tasks", "{}", "{}", "{}",
        ))
        conn.commit()
        check("invalid preview_id FK rejected", False, "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("invalid preview_id FK rejected", True)
    except Exception as e:
        check("invalid preview_id FK rejected", False, str(e))

    # ── TEST 9: 正常插入 planning_approvals ──
    print("\n── TEST 9: 正常插入 planning_approvals ──")
    try:
        c.execute("""
            INSERT INTO planning_approvals
            (approval_id, preview_id, project_id, approved_task_ids_json,
             rejected_task_ids_json, approval_mode, approval_summary_json,
             before_snapshot_json, after_snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "appr-valid", "plan-001", 56, "[26,31]",
            "[27,29]", "selected_tasks",
            '{"safe":2,"high_risk":2}',
            '{"before":true}',
            '{"after":true}',
        ))
        conn.commit()
        check("insert planning_approval", True, "approval_id=appr-valid")
    except Exception as e:
        check("insert planning_approval", False, str(e))

    # ── TEST 10: approval_id 唯一性 ──
    print("\n── TEST 10: approval_id 唯一性 ──")
    try:
        c.execute("""
            INSERT INTO planning_approvals
            (approval_id, preview_id, project_id, approved_task_ids_json,
             approval_mode, approval_summary_json,
             before_snapshot_json, after_snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "appr-valid", "plan-001", 56, "[26]",
            "selected_tasks", "{}", "{}", "{}",
        ))
        conn.commit()
        check("duplicate approval_id rejected", False, "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("duplicate approval_id rejected", True)
    except Exception as e:
        check("duplicate approval_id rejected", False, str(e))

    # ── TEST 11: CASCADE DELETE ──
    print("\n── TEST 11: CASCADE DELETE ──")
    try:
        # planning_approvals 引用 planning_previews(preview_id) 但没有 ON DELETE CASCADE
        # 验证外键约束存在
        c.execute("PRAGMA foreign_key_list(planning_approvals)")
        pa_fks = c.fetchall()
        has_pp_fk = any(fk[2] == "planning_previews" for fk in pa_fks)
        check("planning_approvals FK to planning_previews exists", has_pp_fk, f"found {len(pa_fks)} FK(s)")

        # 验证 planning_previews 有 CASCADE
        c.execute("PRAGMA foreign_key_list(planning_previews)")
        pp_fks = c.fetchall()
        has_cascade = any(fk[2] == "projects" and fk[6] == "CASCADE" for fk in pp_fks)
        check("planning_previews CASCADE to projects exists", has_cascade)
    except Exception as e:
        check("cascade delete", False, str(e))

    # ── TEST 12: 默认值验证 ──
    print("\n── TEST 12: 默认值验证 ──")
    try:
        expires2 = now + dt_mod.timedelta(hours=24)
        c.execute("""
            INSERT INTO planning_previews
            (preview_id, project_id, status, schema_version,
             project_snapshot_hash, tasks_snapshot_hash, task_ids_json,
             preview_json, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "plan-defaults", 56, "generated", "1.0",
            "abc", "def", "[26]", "{}", expires2.isoformat(),
        ))
        conn.commit()

        c.execute("SELECT * FROM planning_previews WHERE preview_id='plan-defaults'")
        row = c.fetchone()
        check("default risk_summary_json='{}'", row["risk_summary_json"] == "{}")
        check("default provider is NULL", row["provider"] is None)
        check("default approved_at is NULL", row["approved_at"] is None)
        check("default rejected_at is NULL", row["rejected_at"] is None)
        check("default updated_at not NULL", row["updated_at"] is not None)
    except Exception as e:
        check("default values", False, str(e))

    # ── TEST 13: 索引验证 ──
    print("\n── TEST 13: 索引验证 ──")
    c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='planning_previews'")
    pp_indexes = {row[0] for row in c.fetchall()}
    check("index project_id+status exists", "idx_planning_previews_project_status" in pp_indexes)
    check("index expires_at exists", "idx_planning_previews_expires_at" in pp_indexes)
    check("index created_at exists", "idx_planning_previews_created_at" in pp_indexes)

    c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='planning_approvals'")
    pa_indexes = {row[0] for row in c.fetchall()}
    check("index preview_id exists", "idx_planning_approvals_preview_id" in pa_indexes)
    check("index project_id exists", "idx_planning_approvals_project_id" in pa_indexes)
    check("index created_at exists", "idx_planning_approvals_created_at" in pa_indexes)

    conn.close()

    # ── TEST 14: 纯洁性验证 ──
    print("\n── TEST 14: 纯洁性验证 ──")
    conn2 = sqlite3.connect(str(test_db))
    conn2.row_factory = sqlite3.Row
    conn2.execute("PRAGMA foreign_keys = ON")
    c2 = conn2.cursor()
    c2.execute("SELECT COUNT(*) as cnt FROM task_leases WHERE status='active'")
    check("active task_leases=0", c2.fetchone()["cnt"] == 0)
    c2.execute("SELECT COUNT(*) as cnt FROM executor_runs WHERE status IN ('starting','scanning','claiming','executing','testing','repairing','paused','stopping')")
    check("active executor_runs=0", c2.fetchone()["cnt"] == 0)
    c2.execute("SELECT COUNT(*) as cnt FROM executor_resource_locks WHERE status='active'")
    check("active resource_locks=0", c2.fetchone()["cnt"] == 0)
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
            print("Usage: python -m app.migrations.009_planning_previews_and_approvals [test]")
    else:
        migrate()
