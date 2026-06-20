"""V2.0-B3a: execution artifact metadata.

Creates the execution_artifacts table required by Result Packet evidence
references. The table stores metadata only; binary payloads stay outside
SQLite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


CREATE_EXECUTION_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS execution_artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id     TEXT NOT NULL,
    result_id       TEXT NOT NULL,
    task_id         INTEGER NOT NULL,
    assignment_id   TEXT NOT NULL DEFAULT '',
    project_id      INTEGER NOT NULL,
    artifact_type   TEXT NOT NULL
                    CHECK (artifact_type IN ('diff','log','test_report','git_commit','screenshot',
                          'build_output','lint_report','coverage_report','binary','document','other')),
    artifact_subtype TEXT,
    storage_path    TEXT NOT NULL,
    storage_url     TEXT,
    content_hash    TEXT,
    size_bytes      INTEGER,
    mime_type       TEXT,
    description     TEXT DEFAULT '',
    tags_json       TEXT DEFAULT '[]',
    is_sensitive    INTEGER DEFAULT 0,
    retention_policy TEXT DEFAULT 'permanent'
                    CHECK (retention_policy IN ('permanent','project_life','30_days','7_days','manual')),
    metadata_json   TEXT DEFAULT '{}',
    expires_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(artifact_id),
    UNIQUE(result_id, artifact_type, artifact_subtype),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS idx_artifacts_result ON execution_artifacts(result_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type   ON execution_artifacts(artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_task   ON execution_artifacts(task_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_assignment ON execution_artifacts(assignment_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_hash   ON execution_artifacts(content_hash);
"""


DROP_EXECUTION_ARTIFACTS = """
DROP TABLE IF EXISTS execution_artifacts;
"""


def upgrade(db_path: str | Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        conn.executescript(CREATE_EXECUTION_ARTIFACTS)
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
        conn.executescript(DROP_EXECUTION_ARTIFACTS)
        conn.commit()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("rollback integrity validation failed")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
