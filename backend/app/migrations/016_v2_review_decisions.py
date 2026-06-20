"""V2.0-B3b: reviewer decision workflow columns.

Rebuilds review_decisions idempotently so it can represent the internal
REVIEWING step plus final Reviewer decisions. Existing rows are preserved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


CREATE_REVIEW_DECISIONS_V2 = """
CREATE TABLE IF NOT EXISTS review_decisions_v2 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       TEXT NOT NULL,
    result_id       TEXT NOT NULL,
    task_id         INTEGER NOT NULL,
    project_id      INTEGER,
    reviewer_type   TEXT NOT NULL
                    CHECK (reviewer_type IN ('auto','human','supervisor','reviewer')),
    reviewer_id     TEXT NOT NULL,
    decision        TEXT NOT NULL
                    CHECK (decision IN ('REVIEWING','PASS','VERIFIED','REWORK','BLOCKED','NEED_USER')),
    reason          TEXT DEFAULT '',
    summary         TEXT DEFAULT '',
    issues_json     TEXT DEFAULT '[]',
    evidence_json   TEXT DEFAULT '{}',
    evidence_refs_json TEXT DEFAULT '[]',
    risk_level      TEXT DEFAULT 'low',
    user_action_required INTEGER DEFAULT 0,
    metadata_json   TEXT DEFAULT '{}',
    rework_steps_json       TEXT DEFAULT '[]',
    rework_deadline         TEXT,
    rework_max_attempts     INTEGER DEFAULT 1,
    blocked_reason          TEXT DEFAULT '',
    blocked_until           TEXT,
    unblock_condition       TEXT DEFAULT '',
    user_prompt             TEXT DEFAULT '',
    user_decision           TEXT,
    user_responded_at       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(review_id),
    UNIQUE(result_id),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id)
);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def upgrade(db_path: str | Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        if _table_exists(conn, "review_decisions"):
            cols = _columns(conn, "review_decisions")
            if {"summary", "issues_json", "evidence_refs_json", "metadata_json", "project_id"}.issubset(cols):
                conn.commit()
            else:
                conn.executescript(CREATE_REVIEW_DECISIONS_V2)
                conn.execute("""
                    INSERT OR IGNORE INTO review_decisions_v2
                    (id, review_id, result_id, task_id, reviewer_type, reviewer_id,
                     decision, reason, evidence_json, rework_steps_json, rework_deadline,
                     rework_max_attempts, blocked_reason, blocked_until, unblock_condition,
                     user_prompt, user_decision, user_responded_at, created_at, updated_at)
                    SELECT id, review_id, result_id, task_id, reviewer_type, reviewer_id,
                           decision, reason, evidence_json, rework_steps_json, rework_deadline,
                           rework_max_attempts, blocked_reason, blocked_until, unblock_condition,
                           user_prompt, user_decision, user_responded_at, created_at, updated_at
                    FROM review_decisions
                """)
                conn.execute("DROP TABLE review_decisions")
                conn.execute("ALTER TABLE review_decisions_v2 RENAME TO review_decisions")
                conn.commit()
        else:
            conn.executescript(CREATE_REVIEW_DECISIONS_V2.replace("review_decisions_v2", "review_decisions"))
            conn.commit()

        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_task ON review_decisions(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_decision ON review_decisions(decision)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_result ON review_decisions(result_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_reviewer ON review_decisions(reviewer_id)")
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or fk_errors:
            raise RuntimeError("migration integrity validation failed")
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
        if _table_exists(conn, "review_decisions"):
            conn.execute("ALTER TABLE review_decisions RENAME TO review_decisions_b3b")
            conn.execute("""
                CREATE TABLE review_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    review_id TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    task_id INTEGER NOT NULL,
                    reviewer_type TEXT NOT NULL CHECK (reviewer_type IN ('auto','human','supervisor')),
                    reviewer_id TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN ('PASS','REWORK','BLOCKED','NEED_USER')),
                    reason TEXT DEFAULT '',
                    evidence_json TEXT DEFAULT '{}',
                    rework_steps_json TEXT DEFAULT '[]',
                    rework_deadline TEXT,
                    rework_max_attempts INTEGER DEFAULT 1,
                    blocked_reason TEXT DEFAULT '',
                    blocked_until TEXT,
                    unblock_condition TEXT DEFAULT '',
                    user_prompt TEXT DEFAULT '',
                    user_decision TEXT,
                    user_responded_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(review_id),
                    FOREIGN KEY (task_id) REFERENCES development_tasks(id)
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO review_decisions
                (id, review_id, result_id, task_id, reviewer_type, reviewer_id,
                 decision, reason, evidence_json, rework_steps_json, rework_deadline,
                 rework_max_attempts, blocked_reason, blocked_until, unblock_condition,
                 user_prompt, user_decision, user_responded_at, created_at, updated_at)
                SELECT id, review_id, result_id, task_id,
                       CASE WHEN reviewer_type='reviewer' THEN 'auto' ELSE reviewer_type END,
                       reviewer_id, decision, reason, evidence_json, rework_steps_json,
                       rework_deadline, rework_max_attempts, blocked_reason, blocked_until,
                       unblock_condition, user_prompt, user_decision, user_responded_at,
                       created_at, updated_at
                FROM review_decisions_b3b
                WHERE decision IN ('PASS','REWORK','BLOCKED','NEED_USER')
            """)
            conn.execute("DROP TABLE review_decisions_b3b")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_task ON review_decisions(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_decision ON review_decisions(decision)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_result ON review_decisions(result_id)")
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("rollback integrity validation failed")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
