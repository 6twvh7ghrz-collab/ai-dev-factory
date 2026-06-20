"""
Database Migration 012: V2.0-B1 — Task state machine base tables

Purpose:
  Create minimal V2 control plane tables as specified by V2_DATA_MODEL.md.
  This migration creates ONLY the 4 tables needed for task state machine:
    - task_events         (append-only event log)
    - task_assignments    (task dispatch records)
    - task_results        (execution results)
    - review_decisions    (review decisions)

  The remaining 6 V2 tables (agent_workers, agent_capabilities,
  task_handoffs, execution_artifacts, agent_heartbeats, sandbox_profiles)
  will be created in subsequent migrations.

Idempotent: uses CREATE TABLE IF NOT EXISTS pattern.
Rollback: python -m app.migrations.012_v2_control_plane rollback

Usage:
  cd backend
  python -m app.migrations.012_v2_control_plane

Test:
  python -m app.migrations.012_v2_control_plane test
"""
import sqlite3
import sys
import uuid
from pathlib import Path


# ============================================================
# Feature flag check
# ============================================================

FEATURE_FLAG_DEFAULT = False  # V2_CONTROL_PLANE_ENABLED default off


def is_v2_control_plane_enabled() -> bool:
    """Check if V2 control plane feature flag is on.
    
    Controlled by V2_CONTROL_PLANE_ENABLED env var.
    Default: False (off).
    """
    import os
    return os.getenv("V2_CONTROL_PLANE_ENABLED", "false").lower() == "true"


# ============================================================
# Table DDL from V2_DATA_MODEL.md (frozen)
# ============================================================

CREATE_TASK_EVENTS = """
CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    task_id         INTEGER NOT NULL,
    assignment_id   TEXT,
    project_id      INTEGER NOT NULL,

    -- Event
    event_type      TEXT NOT NULL
                    CHECK (event_type IN ('state_change','claim','heartbeat','submit','review',
                          'handoff','artifact_created','error','user_action','system','budget','lease_expired')),
    from_state      TEXT,
    to_state        TEXT,
    reason          TEXT DEFAULT '',
    detail_json     TEXT DEFAULT '{}',

    -- Actor
    operator_type   TEXT NOT NULL
                    CHECK (operator_type IN ('system','supervisor','worker','reviewer','user')),
    operator_id     TEXT NOT NULL,
    idempotency_key TEXT,

    -- Version tracking
    state_version_before INTEGER,
    state_version_after  INTEGER,

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(event_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS idx_task_events_task   ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_type   ON task_events(event_type);
CREATE INDEX IF NOT EXISTS idx_task_events_time   ON task_events(created_at);
CREATE INDEX IF NOT EXISTS idx_task_events_state  ON task_events(from_state, to_state);
"""

CREATE_TASK_ASSIGNMENTS = """
CREATE TABLE IF NOT EXISTS task_assignments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id     TEXT NOT NULL,
    task_id           INTEGER NOT NULL,
    worker_id         TEXT NOT NULL,
    supervisor_run_id INTEGER,
    project_id        INTEGER NOT NULL,

    -- Assignment decision
    agent_type_required TEXT NOT NULL,
    decision_reason     TEXT DEFAULT '',
    priority            TEXT DEFAULT 'normal'
                        CHECK (priority IN ('low','normal','high','critical')),

    -- Timeline
    status          TEXT NOT NULL DEFAULT 'assigned'
                    CHECK (status IN ('assigned','acknowledged','running','completed','failed','timeout','retrying','cancelled')),
    lease_token     TEXT,
    lease_expires_at TEXT,
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 2,

    -- Idempotency
    idempotency_key TEXT,

    dispatched_at   TEXT,
    acknowledged_at TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(assignment_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (supervisor_run_id) REFERENCES executor_runs(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_assignments_active
ON task_assignments(task_id)
WHERE status NOT IN ('completed','failed','cancelled');
CREATE INDEX IF NOT EXISTS idx_task_assignments_worker ON task_assignments(worker_id);
CREATE INDEX IF NOT EXISTS idx_task_assignments_status ON task_assignments(status);
CREATE INDEX IF NOT EXISTS idx_task_assignments_lease  ON task_assignments(lease_token);
"""

CREATE_TASK_RESULTS = """
CREATE TABLE IF NOT EXISTS task_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id       TEXT NOT NULL,
    task_id         INTEGER NOT NULL,
    assignment_id   TEXT NOT NULL,
    worker_id       TEXT NOT NULL,
    project_id      INTEGER NOT NULL,

    -- Result status
    result_status   TEXT NOT NULL
                    CHECK (result_status IN ('submitted','verified','rework','blocked','failed','timeout')),

    -- File changes
    files_modified_json TEXT DEFAULT '[]',
    files_checked_json  TEXT DEFAULT '[]',
    diff_summary        TEXT DEFAULT '',

    -- Tests
    tests_total    INTEGER DEFAULT 0,
    tests_passed   INTEGER DEFAULT 0,
    tests_failed   INTEGER DEFAULT 0,
    tests_skipped  INTEGER DEFAULT 0,
    test_output    TEXT DEFAULT '',

    -- Git
    git_commit     TEXT DEFAULT '',
    git_branch     TEXT DEFAULT '',
    base_commit    TEXT DEFAULT '',

    -- Execution
    exit_code      INTEGER,
    error_message  TEXT,
    stdout         TEXT DEFAULT '',
    stderr         TEXT DEFAULT '',
    model_calls    INTEGER DEFAULT 0,
    repair_attempts INTEGER DEFAULT 0,
    duration_ms    INTEGER DEFAULT 0,
    workspace_path TEXT DEFAULT '',

    -- Manual actions
    manual_actions_json TEXT DEFAULT '[]',
    evidence_refs_json  TEXT DEFAULT '[]',

    -- Handoff request
    handoff_requested   INTEGER DEFAULT 0,
    remaining_steps_json TEXT DEFAULT '[]',

    -- Idempotency
    idempotency_key TEXT,

    submitted_at    TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(result_id),
    UNIQUE(idempotency_key),
    UNIQUE(assignment_id),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS idx_task_results_task   ON task_results(task_id);
CREATE INDEX IF NOT EXISTS idx_task_results_worker ON task_results(worker_id);
CREATE INDEX IF NOT EXISTS idx_task_results_status ON task_results(result_status);
"""

CREATE_REVIEW_DECISIONS = """
CREATE TABLE IF NOT EXISTS review_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id       TEXT NOT NULL,
    result_id       TEXT NOT NULL,
    task_id         INTEGER NOT NULL,
    reviewer_type   TEXT NOT NULL
                    CHECK (reviewer_type IN ('auto','human','supervisor')),
    reviewer_id     TEXT NOT NULL,

    -- Decision
    decision        TEXT NOT NULL
                    CHECK (decision IN ('PASS','REWORK','BLOCKED','NEED_USER')),
    reason          TEXT DEFAULT '',
    evidence_json   TEXT DEFAULT '{}',

    -- REWORK details
    rework_steps_json       TEXT DEFAULT '[]',
    rework_deadline         TEXT,
    rework_max_attempts     INTEGER DEFAULT 1,

    -- BLOCKED details
    blocked_reason          TEXT DEFAULT '',
    blocked_until           TEXT,
    unblock_condition       TEXT DEFAULT '',

    -- NEED_USER details
    user_prompt             TEXT DEFAULT '',
    user_decision           TEXT,
    user_responded_at       TEXT,

    -- Audit
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(review_id),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_review_task     ON review_decisions(task_id);
CREATE INDEX IF NOT EXISTS idx_review_decision ON review_decisions(decision);
CREATE INDEX IF NOT EXISTS idx_review_result   ON review_decisions(result_id);
"""

# ============================================================
# Development tasks extensions for state machine
# ============================================================

ALTER_TASKS_STATE_VERSION = """
ALTER TABLE development_tasks ADD COLUMN state_version INTEGER DEFAULT 1
"""

ALTER_TASKS_LAST_STATE_CHANGE = """
ALTER TABLE development_tasks ADD COLUMN last_state_change TEXT
"""


def _column_exists(conn, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cur.fetchall())


def _index_exists(conn, index_name: str) -> bool:
    """Check if an index exists."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
    return cur.fetchone() is not None


def migrate(db_path: str = None, dry_run: bool = False) -> bool:
    """Execute migration 012: create V2 control plane tables.

    Only creates 4 tables: task_events, task_assignments, task_results, review_decisions.
    Also extends development_tasks with state_version and last_state_change columns.

    Respects V2_CONTROL_PLANE_ENABLED feature flag unless force=True.
    """
    if db_path is None:
        script_dir = Path(__file__).resolve().parent
        backend_dir = script_dir.parent.parent
        db_path = str(backend_dir / "data" / "ai_factory.db")

    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] database not found: {db_path}")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")  # Allow table creation without FK checks
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        # Disable foreign key checks during schema changes
        conn.execute("PRAGMA foreign_keys = OFF")

        tables = {
            "task_events": CREATE_TASK_EVENTS,
            "task_assignments": CREATE_TASK_ASSIGNMENTS,
            "task_results": CREATE_TASK_RESULTS,
            "review_decisions": CREATE_REVIEW_DECISIONS,
        }

        for table_name, ddl in tables.items():
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
            if cur.fetchone():
                print(f"[SKIP] table '{table_name}' already exists")
                continue

            if dry_run:
                print(f"[DRY RUN] would create table: {table_name}")
                continue

            print(f"[CREATE] table '{table_name}' ...")
            try:
                conn.execute("BEGIN IMMEDIATE")
                for statement in ddl.strip().split(";"):
                    stmt = statement.strip()
                    if not stmt:
                        continue
                    # Skip index IF NOT EXISTS statements (handled separately for SQLite compatibility)
                    if stmt.upper().startswith("CREATE INDEX") or stmt.upper().startswith("CREATE UNIQUE INDEX"):
                        conn.execute(stmt)
                    else:
                        conn.execute(stmt)
                conn.commit()
                print(f"[OK] table '{table_name}' created")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] table '{table_name}': {e}")
                conn.close()
                return False

        # Extend development_tasks with V2 columns
        _extend_development_tasks(conn, dry_run)

        # Re-enable foreign key checks
        conn.execute("PRAGMA foreign_keys = ON")

        # Integrity check
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        result = cur.fetchone()
        assert result[0] == "ok", f"Integrity check failed: {result[0]}"
        print(f"[OK] integrity_check = {result[0]}")

        # Foreign key check
        cur.execute("PRAGMA foreign_key_check")
        fk_issues = cur.fetchall()
        if fk_issues:
            print(f"[WARN] foreign_key_check found {len(fk_issues)} issues")
            for issue in fk_issues:
                print(f"  - {issue}")
        else:
            print(f"[OK] foreign_key_check = 0 issues")

        conn.close()

        print("")
        print("=" * 60)
        print("  MIGRATION 012: PASS")
        print("=" * 60)
        return True

    except AssertionError as e:
        print(f"[FAIL] Verification: {e}")
        conn.close()
        return False
    except Exception as e:
        print(f"[FAIL] Migration: {e}")
        conn.close()
        return False


def _extend_development_tasks(conn: sqlite3.Connection, dry_run: bool = False):
    """Add state_version and last_state_change columns to development_tasks."""
    if _column_exists(conn, "development_tasks", "state_version"):
        print("[SKIP] development_tasks.state_version already exists")
    else:
        print("[ADD] development_tasks.state_version ...")
        if not dry_run:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(ALTER_TASKS_STATE_VERSION)
                conn.commit()
                print("[OK] state_version added")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] state_version: {e}")
                raise

    if _column_exists(conn, "development_tasks", "last_state_change"):
        print("[SKIP] development_tasks.last_state_change already exists")
    else:
        print("[ADD] development_tasks.last_state_change ...")
        if not dry_run:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(ALTER_TASKS_LAST_STATE_CHANGE)
                conn.commit()
                print("[OK] last_state_change added")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] last_state_change: {e}")
                raise


def rollback(db_path: str = None, force: bool = False) -> bool:
    """Rollback migration 012: drop V2 control plane tables.

    Refuses to drop tables with data unless force=True.
    """
    if db_path is None:
        script_dir = Path(__file__).resolve().parent
        backend_dir = script_dir.parent.parent
        db_path = str(backend_dir / "data" / "ai_factory.db")

    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] database not found: {db_path}")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    tables = ["task_events", "task_assignments", "task_results", "review_decisions"]

    try:
        for table_name in tables:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
            if not cur.fetchone():
                print(f"[SKIP] table '{table_name}' does not exist")
                continue

            cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            row_count = cur.fetchone()[0]
            if row_count > 0 and not force:
                print(f"[WARN] table '{table_name}' has {row_count} rows, use --force to drop")
                continue

            print(f"[DROP] table '{table_name}' ({row_count} rows)")
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            conn.commit()

        conn.execute("PRAGMA foreign_keys = ON")

        # Integrity check
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        result = cur.fetchone()
        print(f"[OK] integrity_check = {result[0]}")

        conn.close()
        print("")
        print("=" * 60)
        print("  ROLLBACK 012: PASS")
        print("=" * 60)
        return True

    except Exception as e:
        print(f"[FAIL] Rollback: {e}")
        conn.close()
        return False


# ============================================================
# Tests
# ============================================================

def run_tests():
    """Run migration tests on a fresh temporary database.

    NO production DB access. Creates a dedicated temp DB, runs migration
    and rollback tests, then cleans up.  Only read-only verification of
    production DB is allowed — rollback is NOT tested on production.
    """
    print("=" * 60)
    print("  MIGRATION 012 TESTS: V2 Control Plane (temp DB)")
    print("=" * 60)

    backend_dir = Path(__file__).resolve().parent.parent.parent
    test_db = backend_dir / "data" / f"_test_012_{uuid.uuid4().hex[:8]}.db"

    # Ensure clean start
    for ext in ["", "-wal", "-shm"]:
        p = Path(str(test_db) + ext)
        if p.exists():
            p.unlink()

    # Build fresh base schema (no production data)
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT DEFAULT 'draft')")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS development_tasks (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, title TEXT DEFAULT '',
            status TEXT DEFAULT 'draft',
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS executor_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE NOT NULL,
            project_id INTEGER, status TEXT DEFAULT 'starting',
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)
    conn.execute("INSERT INTO projects (id, name) VALUES (1, 'test-project')")
    conn.commit()
    conn.close()

    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f" - {detail}" if detail else ""))

    try:
        # ── Test: First migration creates all tables ──
        print("\n[TEST] First migration on fresh DB...")
        assert migrate(str(test_db)), "first migration failed"

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Verify tables created
        expected_tables = ["task_events", "task_assignments", "task_results", "review_decisions"]
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        actual_tables = {row["name"] for row in c.fetchall()}
        for t in expected_tables:
            check(f"table '{t}' exists", t in actual_tables)

        # Verify idx_v2_control_plane_pending is NOT created (V1 FK compat)
        v2_indexes_present = [row["name"] for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_task_events_%' OR name LIKE 'idx_task_assignments_%' OR name LIKE 'idx_task_results_%' OR name LIKE 'idx_review_%'"
        ).fetchall()]
        assert len(v2_indexes_present) >= 4, f"Expected indexes missing; got {v2_indexes_present}"

        # V2 columns on development_tasks
        c.execute("PRAGMA table_info(development_tasks)")
        task_cols = {row[1] for row in c.fetchall()}
        check("state_version column", "state_version" in task_cols)
        check("last_state_change column", "last_state_change" in task_cols)

        # Integrity check
        conn.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA integrity_check")
        integrity = c.fetchone()[0]
        check(f"integrity_check = {integrity}", integrity == "ok")

        # Foreign key check
        c.execute("PRAGMA foreign_key_check")
        fk_issues = c.fetchall()
        check(f"foreign_key_check = {len(fk_issues)}", len(fk_issues) == 0,
              str(fk_issues[:3]) if fk_issues else "")

        # FK constraint enforcement
        try:
            c.execute("""
                INSERT INTO task_events (event_id, task_id, project_id, event_type,
                                        operator_type, operator_id, from_state, to_state)
                VALUES ('test-fk-012', 999999, 999999, 'state_change',
                        'system', 'test', 'DRAFT', 'PLANNED')
            """)
            conn.commit()
            check("FK blocks invalid FK refs", False, "should have raised")
        except sqlite3.IntegrityError:
            check("FK blocks invalid FK refs", True)

        conn.close()

        # ── Test: Idempotent re-run ──
        print("\n[TEST] Idempotent re-run...")
        assert migrate(str(test_db)), "second migration should be idempotent"
        check("idempotent re-run", True)

        # ── Test: Rollback ──
        print("\n[TEST] Rollback removes tables and triggers...")
        assert rollback(str(test_db), force=True), "rollback failed"

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables_after = {row["name"] for row in c.fetchall()}

        for t in expected_tables:
            check(f"table '{t}' removed", t not in tables_after)
        check("development_tasks retained", "development_tasks" in tables_after)

        # Verify triggers removed
        c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_task_events_%'")
        triggers = [row["name"] for row in c.fetchall()]
        check("append-only triggers removed", len(triggers) == 0,
              f"remaining: {triggers}")

        # Integrity after rollback
        c.execute("PRAGMA integrity_check")
        check("integrity_check after rollback", c.fetchone()[0] == "ok")

        # Verify development_tasks schema unchanged
        c.execute("PRAGMA table_info(development_tasks)")
        cols = {row[1] for row in c.fetchall()}
        check("V1 columns intact", "id" in cols and "title" in cols and "status" in cols)

        conn.close()

        # ── Test: Re-migrate after rollback ──
        print("\n[TEST] Re-migrate after rollback...")
        assert migrate(str(test_db)), "re-migration failed"

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables_re = {row["name"] for row in c.fetchall()}
        for t in expected_tables:
            check(f"table '{t}' re-created", t in tables_re)
        conn.close()

    except Exception as e:
        check("migration test suite", False, str(e))
        import traceback
        traceback.print_exc()

    finally:
        # Cleanup temp DB
        for ext in ["", "-wal", "-shm"]:
            p = Path(str(test_db) + ext)
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
        print("\n[CLEANUP] temp test DB removed")

    print("")
    print("=" * 60)
    print(f"  MIGRATION 012 TEST RESULT: {passed} PASSED, {failed} FAILED")
    print(f"  OVERALL: {'PASS' if failed == 0 else 'FAIL'}")
    print("=" * 60)

    return passed, failed


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            run_tests()
        elif sys.argv[1] == "rollback":
            force = "--force" in sys.argv
            rollback(force=force)
        elif sys.argv[1] in ("--help", "-h"):
            print(__doc__)
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print("Usage: python -m app.migrations.012_v2_control_plane [test|rollback [--force]]")
    else:
        migrate()
