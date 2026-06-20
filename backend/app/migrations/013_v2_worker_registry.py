"""
Database Migration 013: V2.0-B2 — Worker registry and heartbeat tables

Purpose:
  Create tables for V2 worker management:
    - agent_workers        (worker registration and status)
    - agent_capabilities   (worker capability tags)
    - agent_heartbeats     (task heartbeat / lease renewal log)
    - sandbox_profiles     (execution environment profiles)

  Reuses B1 tables:
    - task_assignments     (created in 012)
    - task_events          (created in 012)
    - development_tasks.state_version (added in 012)

Idempotent: uses CREATE TABLE IF NOT EXISTS pattern.
Rollback: python -m app.migrations.013_v2_worker_registry rollback

Usage:
  cd backend
  python -m app.migrations.013_v2_worker_registry

Test:
  python -m app.migrations.013_v2_worker_registry test
"""
import sqlite3
import sys
import uuid
from pathlib import Path


# ============================================================
# Table DDL
# ============================================================

CREATE_AGENT_WORKERS = """
CREATE TABLE IF NOT EXISTS agent_workers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id         TEXT NOT NULL,
    worker_type       TEXT NOT NULL
                      CHECK (worker_type IN ('executor','supervisor','reviewer')),
    provider          TEXT DEFAULT '',
    display_name      TEXT DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'registered'
                      CHECK (status IN ('registered','available','busy','offline','disabled')),
    max_concurrency   INTEGER DEFAULT 1,
    current_load      INTEGER DEFAULT 0,
    sandbox_profile_id TEXT DEFAULT '',
    registered_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json     TEXT DEFAULT '{}',
    version           INTEGER DEFAULT 1,

    UNIQUE(worker_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_workers_active_executor
ON agent_workers(worker_type, status)
WHERE worker_type = 'executor' AND status IN ('available','busy');

CREATE INDEX IF NOT EXISTS idx_agent_workers_status ON agent_workers(status);
CREATE INDEX IF NOT EXISTS idx_agent_workers_type   ON agent_workers(worker_type);
"""

CREATE_AGENT_CAPABILITIES = """
CREATE TABLE IF NOT EXISTS agent_capabilities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    TEXT NOT NULL,
    capability   TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(worker_id, capability),
    FOREIGN KEY (worker_id) REFERENCES agent_workers(worker_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_cap_worker ON agent_capabilities(worker_id);
CREATE INDEX IF NOT EXISTS idx_agent_cap_name   ON agent_capabilities(capability);
"""

CREATE_AGENT_HEARTBEATS = """
CREATE TABLE IF NOT EXISTS agent_heartbeats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    heartbeat_id    TEXT NOT NULL,
    worker_id       TEXT NOT NULL,
    task_id         INTEGER NOT NULL,
    assignment_id   TEXT NOT NULL,
    lease_token     TEXT NOT NULL,
    idempotency_key TEXT,
    renewed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(heartbeat_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY (worker_id) REFERENCES agent_workers(worker_id),
    FOREIGN KEY (assignment_id) REFERENCES task_assignments(assignment_id),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_hb_worker     ON agent_heartbeats(worker_id);
CREATE INDEX IF NOT EXISTS idx_agent_hb_task       ON agent_heartbeats(task_id);
CREATE INDEX IF NOT EXISTS idx_agent_hb_assignment ON agent_heartbeats(assignment_id);
"""

CREATE_SANDBOX_PROFILES = """
CREATE TABLE IF NOT EXISTS sandbox_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      TEXT NOT NULL,
    profile_name    TEXT NOT NULL DEFAULT 'default',
    description     TEXT DEFAULT '',
    allowed_files_json  TEXT DEFAULT '[]',
    forbidden_actions_json TEXT DEFAULT '[]',
    test_commands_json   TEXT DEFAULT '[]',
    success_criteria_json TEXT DEFAULT '[]',
    evidence_required_json TEXT DEFAULT '[]',
    metadata_json   TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE(profile_id)
);

CREATE INDEX IF NOT EXISTS idx_sandbox_profile_name ON sandbox_profiles(profile_name);
"""


# ============================================================
# Helper functions
# ============================================================

def _index_exists(conn, index_name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
    return cur.fetchone() is not None


# ============================================================
# Migrate
# ============================================================

def migrate(db_path: str = None, dry_run: bool = False) -> bool:
    """Execute migration 013: create V2 worker registry tables."""
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
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        tables = {
            "agent_workers": CREATE_AGENT_WORKERS,
            "agent_capabilities": CREATE_AGENT_CAPABILITIES,
            "agent_heartbeats": CREATE_AGENT_HEARTBEATS,
            "sandbox_profiles": CREATE_SANDBOX_PROFILES,
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
                    conn.execute(stmt)
                conn.commit()
                print(f"[OK] table '{table_name}' created")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] table '{table_name}': {e}")
                conn.close()
                return False

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
        print("  MIGRATION 013: PASS")
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


# ============================================================
# Rollback
# ============================================================

def rollback(db_path: str = None, force: bool = False) -> bool:
    """Rollback migration 013: drop V2 worker registry tables."""
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

    # Drop in reverse dependency order (FK constraints)
    tables = ["agent_heartbeats", "agent_capabilities", "agent_workers", "sandbox_profiles"]

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
        print("  ROLLBACK 013: PASS")
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
    """Run migration tests on a fresh temporary database."""
    print("=" * 60)
    print("  MIGRATION 013 TESTS: V2 Worker Registry (temp DB)")
    print("=" * 60)

    backend_dir = Path(__file__).resolve().parent.parent.parent
    test_db = backend_dir / "data" / f"_test_013_{uuid.uuid4().hex[:8]}.db"

    # Ensure clean start
    for ext in ["", "-wal", "-shm"]:
        p = Path(str(test_db) + ext)
        if p.exists():
            p.unlink()

    # Build fresh base schema with required FK-referenced tables
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT DEFAULT 'draft'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS development_tasks (
            id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, title TEXT DEFAULT '',
            status TEXT DEFAULT 'draft', state_version INTEGER DEFAULT 1,
            last_state_change TEXT,
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
    # task_assignments needed for agent_heartbeats FK
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            supervisor_run_id INTEGER,
            project_id INTEGER NOT NULL,
            agent_type_required TEXT NOT NULL DEFAULT 'executor',
            decision_reason TEXT DEFAULT '',
            priority TEXT DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'assigned',
            lease_token TEXT,
            lease_expires_at TEXT,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 2,
            idempotency_key TEXT UNIQUE,
            dispatched_at TEXT,
            acknowledged_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (task_id) REFERENCES development_tasks(id),
            FOREIGN KEY (project_id) REFERENCES projects(id),
            FOREIGN KEY (supervisor_run_id) REFERENCES executor_runs(id)
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

        expected_tables = ["agent_workers", "agent_capabilities", "agent_heartbeats", "sandbox_profiles"]
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        actual_tables = {row["name"] for row in c.fetchall()}
        for t in expected_tables:
            check(f"table '{t}' exists", t in actual_tables)

        # Verify agent_workers schema
        c.execute("PRAGMA table_info(agent_workers)")
        worker_cols = {row[1] for row in c.fetchall()}
        for col in ["worker_id", "worker_type", "status", "max_concurrency", "current_load",
                     "registered_at", "last_seen_at", "version"]:
            check(f"agent_workers.{col} column", col in worker_cols)

        # Verify unique index on active executor workers
        c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_agent_workers_active_executor'")
        check("active executor unique index exists", c.fetchone() is not None)

        # Verify agent_capabilities FK
        c.execute("PRAGMA table_info(agent_capabilities)")
        cap_cols = {row[1] for row in c.fetchall()}
        check("agent_capabilities.worker_id column", "worker_id" in cap_cols)
        check("agent_capabilities.capability column", "capability" in cap_cols)

        # Verify agent_heartbeats schema
        c.execute("PRAGMA table_info(agent_heartbeats)")
        hb_cols = {row[1] for row in c.fetchall()}
        for col in ["heartbeat_id", "worker_id", "task_id", "assignment_id", "lease_token",
                     "idempotency_key"]:
            check(f"agent_heartbeats.{col} column", col in hb_cols)

        # Verify sandbox_profiles schema
        c.execute("PRAGMA table_info(sandbox_profiles)")
        sp_cols = {row[1] for row in c.fetchall()}
        for col in ["profile_id", "profile_name", "allowed_files_json", "forbidden_actions_json"]:
            check(f"sandbox_profiles.{col} column", col in sp_cols)

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

        # FK constraint: agent_workers FK to non-existent worker should fail on agent_capabilities
        try:
            c.execute("INSERT INTO agent_capabilities (worker_id, capability) VALUES ('nonexistent', 'test')")
            conn.commit()
            check("FK blocks invalid worker_id in agent_capabilities", False, "should have raised")
        except sqlite3.IntegrityError:
            check("FK blocks invalid worker_id in agent_capabilities", True)

        # FK constraint: agent_heartbeats->agent_workers
        try:
            c.execute("""
                INSERT INTO agent_heartbeats (heartbeat_id, worker_id, task_id, assignment_id, lease_token)
                VALUES ('hb-fk-test', 'nonexistent', 999, 'asgn-999', 'tok-999')
            """)
            conn.commit()
            check("FK blocks invalid worker_id in agent_heartbeats", False, "should have raised")
        except sqlite3.IntegrityError:
            check("FK blocks invalid worker_id in agent_heartbeats", True)

        conn.close()

        # ── Test: Idempotent re-run ──
        print("\n[TEST] Idempotent re-run...")
        assert migrate(str(test_db)), "second migration should be idempotent"
        check("idempotent re-run", True)

        # ── Test: Rollback ──
        print("\n[TEST] Rollback removes tables...")
        assert rollback(str(test_db), force=True), "rollback failed"

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables_after = {row["name"] for row in c.fetchall()}

        for t in expected_tables:
            check(f"table '{t}' removed", t not in tables_after)

        c.execute("PRAGMA integrity_check")
        check("integrity_check after rollback", c.fetchone()[0] == "ok")
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
    print(f"  MIGRATION 013 TEST RESULT: {passed} PASSED, {failed} FAILED")
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
            print("Usage: python -m app.migrations.013_v2_worker_registry [test|rollback [--force]]")
    else:
        migrate()
