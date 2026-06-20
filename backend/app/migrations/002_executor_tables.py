"""
数据库迁移：执行器基础表

新增表：
  1. executions       - 执行会话记录
  2. execution_logs   - 执行步骤日志
  3. task_leases      - 任务原子领取锁

修改表：
  bugs 新增 task_id / execution_id / repair_attempt / checkpoint 字段

使用方法：
  cd backend
  python -m app.migrations.002_executor_tables

回滚：
  删除 executions / execution_logs / task_leases 表
  删除 bugs 表新增字段（SQLite 不支持 DROP COLUMN，回滚需重建）

幂等策略：
  全部使用 IF NOT EXISTS / 检查 PRAGMA table_info 后决定是否执行
  重复执行安全
"""
import sqlite3
from pathlib import Path
from datetime import datetime


def migrate(db_path: str = "data/ai_factory.db"):
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] 数据库文件不存在: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    # ═══════════════════════════════════════════
    # 1. executions 表
    # ═══════════════════════════════════════════
    cur.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL,
            project_id      INTEGER NOT NULL,
            worker_id       VARCHAR(50) NOT NULL DEFAULT 'worker-default',
            status          VARCHAR(20) NOT NULL DEFAULT 'pending',
            -- 状态枚举: pending / running / testing / success / failed / blocked / cancelled

            -- Worktree 信息
            worktree_path   TEXT,
            worktree_branch TEXT,
            start_commit    TEXT,  -- 执行开始时的 HEAD commit

            -- 执行统计
            started_at      DATETIME,
            completed_at    DATETIME,
            duration_ms     INTEGER DEFAULT 0,

            -- 修复统计
            repair_count    INTEGER DEFAULT 0,
            max_repairs     INTEGER DEFAULT 2,

            -- 结果
            exit_code        INTEGER,
            test_result      TEXT,   -- pass / fail / not_run
            execution_result TEXT,   -- JSON: 详细结果摘要
            error_message    TEXT,

            -- 安全检查
            safety_passed    INTEGER DEFAULT 0,  -- 0=未检查 1=通过 -1=不通过
            files_checked    TEXT,   -- JSON: 检查的文件列表
            files_modified   TEXT,   -- JSON: 实际修改的文件列表

            -- 预算
            model_calls      INTEGER DEFAULT 0,

            created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (task_id) REFERENCES development_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    print("[OK] executions table ready")

    # 索引
    cur.execute("CREATE INDEX IF NOT EXISTS ix_executions_task_id ON executions(task_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_executions_status ON executions(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_executions_worker_id ON executions(worker_id)")
    print("[OK] executions indexes ready")

    # ═══════════════════════════════════════════
    # 2. execution_logs 表
    # ═══════════════════════════════════════════
    cur.execute("""
        CREATE TABLE IF NOT EXISTS execution_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id    INTEGER NOT NULL,
            step_name       VARCHAR(100) NOT NULL,
            -- 步骤名: claim_task / check_git / create_checkpoint / create_worktree /
            --         run_command / safety_check / run_test / auto_repair / merge / cleanup

            step_status     VARCHAR(20) NOT NULL DEFAULT 'running',
            -- running / success / failed / skipped

            command         TEXT,
            stdout          TEXT,
            stderr          TEXT,
            exit_code       INTEGER,
            duration_ms     INTEGER DEFAULT 0,
            detail          TEXT,  -- JSON: 附加信息

            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (execution_id) REFERENCES executions(id) ON DELETE CASCADE
        )
    """)
    print("[OK] execution_logs table ready")

    cur.execute("CREATE INDEX IF NOT EXISTS ix_execution_logs_exec_id ON execution_logs(execution_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_execution_logs_step ON execution_logs(step_name)")
    print("[OK] execution_logs indexes ready")

    # ═══════════════════════════════════════════
    # 3. task_leases 表 (原子领取锁)
    # ═══════════════════════════════════════════
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_leases (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id         INTEGER NOT NULL UNIQUE,
            execution_id    INTEGER,
            worker_id       VARCHAR(50) NOT NULL,
            status          VARCHAR(20) NOT NULL DEFAULT 'active',
            -- active / released / expired

            locked_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at      DATETIME,
            released_at     DATETIME,

            FOREIGN KEY (task_id) REFERENCES development_tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (execution_id) REFERENCES executions(id) ON DELETE SET NULL
        )
    """)
    print("[OK] task_leases table ready")

    cur.execute("CREATE INDEX IF NOT EXISTS ix_task_leases_status ON task_leases(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_task_leases_expires ON task_leases(expires_at)")
    print("[OK] task_leases indexes ready")

    # ═══════════════════════════════════════════
    # 4. bugs 表新增字段 (幂等)
    # ═══════════════════════════════════════════
    cur.execute("PRAGMA table_info(bugs)")
    existing_cols = {row[1] for row in cur.fetchall()}

    bug_new_columns = {
        "task_id": "INTEGER",
        "execution_id": "INTEGER",
        "repair_attempt": "INTEGER DEFAULT 0",
        "checkpoint": "TEXT",
    }

    for col, col_type in bug_new_columns.items():
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE bugs ADD COLUMN {col} {col_type}")
            print(f"[ADD] bugs.{col} ({col_type})")
        else:
            print(f"[OK] bugs.{col} already exists")

    conn.commit()
    conn.close()
    print(f"\n[DONE] 迁移完成 - {datetime.now().isoformat()}")


def rollback(db_path: str = "data/ai_factory.db"):
    """回滚脚本：删除新增表，bugs 字段在 SQLite 中无法直接删除"""
    db_path = Path(db_path)
    if not db_path.exists():
        print("[SKIP] 数据库不存在")
        return

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()

    for table in ["task_leases", "execution_logs", "executions"]:
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        print(f"[DROP] {table}")

    conn.commit()
    conn.close()
    print("[DONE] 回滚完成（bugs 新增字段需手动处理）")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "rollback":
        rollback()
    else:
        migrate()
