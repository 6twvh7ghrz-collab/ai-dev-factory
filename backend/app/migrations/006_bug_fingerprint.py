"""
数据库迁移 006：Bug 任务关联字段 + 去重指纹

新增:
- bugs.task_id: 关联开发任务ID
- bugs.execution_id: 关联执行记录ID
- bugs.repair_attempt: 修复尝试次数
- bugs.failure_fingerprint: 去重指纹 (sha256[:32])
- 部分唯一索引: uq_bug_dedup  on (task_id, execution_id, failure_fingerprint)

使用方法:
  cd backend
  python -m app.migrations.006_bug_fingerprint
"""
import sqlite3
from pathlib import Path


def migrate(db_path: str = "data/app.db"):
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] 数据库文件不存在: {db_path}")
        return

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # 获取 bugs 表现有列
    cur.execute("PRAGMA table_info(bugs)")
    existing_cols = {row[1] for row in cur.fetchall()}

    new_columns = {
        "task_id": "INTEGER",
        "execution_id": "INTEGER",
        "repair_attempt": "INTEGER DEFAULT 0",
        "failure_fingerprint": "VARCHAR(32)",
    }

    for col, col_type in new_columns.items():
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE bugs ADD COLUMN {col} {col_type}")
            print(f"[ADD] bugs.{col}")
        else:
            print(f"[OK]  bugs.{col} already exists")

    # 创建去重唯一索引（部分索引：只对非 NULL fingerprint 生效）
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_bug_dedup
        ON bugs(task_id, execution_id, failure_fingerprint)
        WHERE failure_fingerprint IS NOT NULL
    """)
    print("[OK]  uq_bug_dedup index ready")

    # 创建 task_id + execution_id 查询索引
    cur.execute("CREATE INDEX IF NOT EXISTS ix_bugs_task_exec ON bugs(task_id, execution_id)")
    print("[OK]  ix_bugs_task_exec index ready")

    conn.commit()
    conn.close()
    print("[DONE] 迁移 006 完成")


if __name__ == "__main__":
    migrate()
