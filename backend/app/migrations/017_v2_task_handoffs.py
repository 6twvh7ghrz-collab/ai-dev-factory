"""V2.0-B4: task handoff packets and transfer workflow."""

from __future__ import annotations

import sqlite3
from pathlib import Path


CREATE_TASK_HANDOFFS = """
CREATE TABLE IF NOT EXISTS task_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    from_assignment_id TEXT NOT NULL,
    from_worker_id TEXT NOT NULL,
    to_worker_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','accepted','rejected','cancelled','expired')),
    reason_code TEXT NOT NULL,
    reason TEXT DEFAULT '',
    current_task_state TEXT DEFAULT '',
    current_stage TEXT DEFAULT '',
    completed_steps_json TEXT DEFAULT '[]',
    remaining_steps_json TEXT DEFAULT '[]',
    files_changed_json TEXT DEFAULT '[]',
    tests_run_json TEXT DEFAULT '[]',
    recent_errors_json TEXT DEFAULT '[]',
    evidence_refs_json TEXT DEFAULT '[]',
    forbidden_actions_json TEXT DEFAULT '[]',
    context_snapshot_json TEXT DEFAULT '{}',
    git_head TEXT DEFAULT '',
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    rejected_at TEXT,
    cancelled_at TEXT,
    expired_at TEXT,
    idempotency_key TEXT,
    request_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(handoff_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS idx_handoffs_task ON task_handoffs(task_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_project ON task_handoffs(project_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_from_assignment ON task_handoffs(from_assignment_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_from_worker ON task_handoffs(from_worker_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_to_worker ON task_handoffs(to_worker_id);
CREATE INDEX IF NOT EXISTS idx_handoffs_status ON task_handoffs(status);
CREATE INDEX IF NOT EXISTS idx_handoffs_expires ON task_handoffs(expires_at);
"""


def upgrade(db_path: str | Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        for statement in CREATE_TASK_HANDOFFS.split(";"):
            if statement.strip():
                conn.execute(statement)
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or fk_errors:
            raise RuntimeError("handoff migration validation failed")
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
        conn.execute("DROP TABLE IF EXISTS task_handoffs")
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("handoff rollback validation failed")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
