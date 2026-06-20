"""
数据库迁移：Bug 表新增字段 + BugStatusLog 新表

使用方法：
  cd backend
  python -m app.migrations.001_bug_lifecycle
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
        "is_blocking": "TEXT DEFAULT 'unknown'",
        "execution_result": "TEXT",
        "files_changed": "TEXT",
        "test_result": "TEXT",
        "remaining_issues": "TEXT",
        "executed_at": "DATETIME",
        "resolved_at": "DATETIME",
    }

    for col, col_type in new_columns.items():
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE bugs ADD COLUMN {col} {col_type}")
            print(f"[ADD] bugs.{col}")
        else:
            print(f"[OK]  bugs.{col} already exists")

    # 创建 bug_status_logs 表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bug_status_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bug_id INTEGER NOT NULL,
            from_status VARCHAR(20),
            to_status VARCHAR(20) NOT NULL,
            reason TEXT,
            operator VARCHAR(100),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (bug_id) REFERENCES bugs(id) ON DELETE CASCADE
        )
    """)
    print("[OK]  bug_status_logs table ready")

    # 创建索引
    cur.execute("CREATE INDEX IF NOT EXISTS ix_bug_status_logs_bug_id ON bug_status_logs(bug_id)")
    print("[OK]  bug_status_logs index ready")

    conn.commit()
    conn.close()
    print("[DONE] 迁移完成")


if __name__ == "__main__":
    migrate()
