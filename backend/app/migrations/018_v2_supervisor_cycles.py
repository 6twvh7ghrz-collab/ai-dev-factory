"""V2.0-B5a: deterministic supervisor cycle audit."""

from __future__ import annotations

import sqlite3
from pathlib import Path


CREATE_SUPERVISOR_CYCLES = """
CREATE TABLE IF NOT EXISTS supervisor_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL UNIQUE,
    project_id INTEGER NOT NULL,
    task_id INTEGER,
    observed_state TEXT DEFAULT '',
    state_version INTEGER,
    planned_action TEXT NOT NULL,
    selected_actor_id TEXT DEFAULT '',
    dry_run INTEGER NOT NULL DEFAULT 0,
    result TEXT DEFAULT '',
    result_json TEXT DEFAULT '{}',
    idempotency_key TEXT UNIQUE,
    request_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_supervisor_cycles_project ON supervisor_cycles(project_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_cycles_task ON supervisor_cycles(task_id);
CREATE INDEX IF NOT EXISTS idx_supervisor_cycles_action ON supervisor_cycles(planned_action);
CREATE INDEX IF NOT EXISTS idx_supervisor_cycles_created ON supervisor_cycles(created_at);
"""


def upgrade(db_path: str | Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        for statement in CREATE_SUPERVISOR_CYCLES.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or fk_errors:
            raise RuntimeError("supervisor cycle migration validation failed")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def downgrade(db_path: str | Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS supervisor_cycles")
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("supervisor cycle rollback validation failed")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
