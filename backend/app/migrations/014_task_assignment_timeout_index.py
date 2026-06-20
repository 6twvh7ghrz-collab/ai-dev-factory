"""
Database Migration 014: V2.0-B2b-R — Fix timeout assignment unique index

Purpose:
  The active assignment UNIQUE INDEX in Migration 012 excluded only
  ('completed','failed','cancelled'), which meant timeout assignments
  remained in the unique index and blocked new claims.  This migration
  rebuilds the index to also exclude 'timeout'.

  Old (012):
    WHERE status NOT IN ('completed','failed','cancelled')

  New (014):
    WHERE status NOT IN ('completed','failed','cancelled','timeout')

Behavior:
  1. Check task_assignments table exists
  2. Read current idx_task_assignments_active definition
  3. Drop old index
  4. Recreate with 'timeout' excluded
  5. No assignment history modified
  6. No assignment statuses changed

Idempotent: checks new index definition before acting; skips if already correct.
Rollback: python -m app.migrations.014_task_assignment_timeout_index rollback
           (restores the 012-era index without 'timeout')

Usage:
  cd backend
  python -m app.migrations.014_task_assignment_timeout_index

Test:
  python -m app.migrations.014_task_assignment_timeout_index test
"""
import sqlite3
import sys
import uuid
from pathlib import Path


# ============================================================
# Index definitions
# ============================================================

INDEX_NAME = "idx_task_assignments_active"

OLD_WHERE = "status NOT IN ('completed','failed','cancelled')"
NEW_WHERE = "status NOT IN ('completed','failed','cancelled','timeout')"

CREATE_INDEX_OLD = f"""CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
ON task_assignments(task_id)
WHERE {OLD_WHERE}"""

CREATE_INDEX_NEW = f"""CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
ON task_assignments(task_id)
WHERE {NEW_WHERE}"""


# ============================================================
# Helpers
# ============================================================

def _table_exists(conn, table_name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return cur.fetchone() is not None


def _index_exists(conn, index_name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
    return cur.fetchone() is not None


def _get_index_sql(conn, index_name: str) -> str | None:
    """Return the CREATE SQL for an index, or None if not found."""
    cur = conn.cursor()
    cur.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    return None


def _index_has_timeout(index_sql: str) -> bool:
    """Check if index WHERE clause already includes 'timeout'."""
    import re
    # Look for 'timeout' inside the WHERE clause
    where_match = re.search(r'WHERE\s+(.*)', index_sql, re.IGNORECASE)
    if where_match:
        where_clause = where_match.group(1)
        return "'timeout'" in where_clause
    return False


# ============================================================
# Migrate
# ============================================================

def migrate(db_path: str = None, dry_run: bool = False) -> bool:
    """Execute migration 014: rebuild active assignment index to exclude 'timeout'.

    Only touches idx_task_assignments_active partial unique index.
    No data rows are modified or deleted.
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
    conn.execute("PRAGMA journal_mode = WAL")

    try:
        # 1. Check table exists
        if not _table_exists(conn, "task_assignments"):
            print("[SKIP] task_assignments table does not exist")
            conn.close()
            return True  # not an error — nothing to do

        # 2. Check current index state
        current_sql = _get_index_sql(conn, INDEX_NAME)

        if current_sql and _index_has_timeout(current_sql):
            print(f"[SKIP] index '{INDEX_NAME}' already excludes 'timeout' (idempotent)")
            # Still verify integrity
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            result = cur.fetchone()
            assert result[0] == "ok", f"Integrity check failed: {result[0]}"
            print(f"[OK] integrity_check = {result[0]}")
            cur.execute("PRAGMA foreign_key_check")
            fk_issues = cur.fetchall()
            print(f"[OK] foreign_key_check = {len(fk_issues)} issues")
            conn.close()
            return True

        if dry_run:
            print(f"[DRY RUN] Would rebuild '{INDEX_NAME}' to exclude 'timeout'")
            if current_sql:
                print(f"  Current: {current_sql}")
            else:
                print(f"  Index does not exist yet")
            conn.close()
            return True

        # 3. Report old index definition
        if current_sql:
            print(f"[INFO] Old index definition:")
            print(f"  {current_sql}")
        else:
            print(f"[INFO] Index '{INDEX_NAME}' does not exist — will create new")

        # 4. Drop old index + recreate inside transaction
        conn.execute("BEGIN IMMEDIATE")

        if current_sql and not _index_has_timeout(current_sql):
            conn.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
            print(f"[DROP] old '{INDEX_NAME}'")

        conn.execute(CREATE_INDEX_NEW)
        print(f"[CREATE] '{INDEX_NAME}' with timeout excluded")
        conn.commit()

        # 5. Verify
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()

        new_sql = _get_index_sql(conn, INDEX_NAME)
        print(f"[INFO] New index definition:")
        print(f"  {new_sql}")

        if not _index_has_timeout(new_sql):
            print(f"[FAIL] New index does NOT exclude 'timeout'!")
            conn.close()
            return False

        cur.execute("PRAGMA integrity_check")
        result = cur.fetchone()
        assert result[0] == "ok", f"Integrity check failed: {result[0]}"
        print(f"[OK] integrity_check = {result[0]}")

        cur.execute("PRAGMA foreign_key_check")
        fk_issues = cur.fetchall()
        if fk_issues:
            print(f"[WARN] foreign_key_check found {len(fk_issues)} issues")
        else:
            print(f"[OK] foreign_key_check = 0 issues")

        conn.close()

        print("")
        print("=" * 60)
        print("  MIGRATION 014: PASS")
        print("=" * 60)
        return True

    except AssertionError as e:
        print(f"[FAIL] Verification: {e}")
        conn.close()
        return False
    except Exception as e:
        print(f"[FAIL] Migration: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return False


# ============================================================
# Rollback
# ============================================================

def rollback(db_path: str = None, force: bool = False) -> bool:
    """Rollback migration 014: restore old index without 'timeout'.

    Drops the new index and recreates the 012-era definition.
    No assignment data rows are modified.
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

    try:
        if not _table_exists(conn, "task_assignments"):
            print("[SKIP] task_assignments table does not exist")
            conn.close()
            return True

        current_sql = _get_index_sql(conn, INDEX_NAME)

        if current_sql and not _index_has_timeout(current_sql):
            print(f"[SKIP] index already in 012-era state (no 'timeout')")
            conn.close()
            return True

        conn.execute("BEGIN IMMEDIATE")

        if current_sql and _index_has_timeout(current_sql):
            conn.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
            print(f"[DROP] new '{INDEX_NAME}' (with timeout)")

        conn.execute(CREATE_INDEX_OLD)
        print(f"[CREATE] restored 012-era '{INDEX_NAME}' (no timeout)")

        conn.commit()

        # Verify
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()

        restored_sql = _get_index_sql(conn, INDEX_NAME)
        print(f"[INFO] Restored index:")
        print(f"  {restored_sql}")

        cur.execute("PRAGMA integrity_check")
        result = cur.fetchone()
        assert result[0] == "ok", f"Integrity check failed: {result[0]}"
        print(f"[OK] integrity_check = {result[0]}")

        cur.execute("PRAGMA foreign_key_check")
        fk_issues = cur.fetchall()
        print(f"[OK] foreign_key_check = {len(fk_issues)} issues")

        conn.close()

        print("")
        print("=" * 60)
        print("  ROLLBACK 014: PASS")
        print("=" * 60)
        return True

    except AssertionError as e:
        print(f"[FAIL] Verification: {e}")
        conn.close()
        return False
    except Exception as e:
        print(f"[FAIL] Rollback: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return False


# ============================================================
# Tests (all on temp DB — NO production DB access)
# ============================================================

def run_tests():
    """Run migration 014 tests on a fresh temporary database."""
    print("=" * 60)
    print("  MIGRATION 014 TESTS: Timeout Index (temp DB)")
    print("=" * 60)

    backend_dir = Path(__file__).resolve().parent.parent.parent
    test_db = backend_dir / "data" / f"_test_014_{uuid.uuid4().hex[:8]}.db"

    # Ensure clean start
    for ext in ["", "-wal", "-shm"]:
        p = Path(str(test_db) + ext)
        if p.exists():
            p.unlink()

    # Build fresh base schema (simulating post-012 state)
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = OFF")

    conn.execute("CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, name TEXT)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS development_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title TEXT DEFAULT '',
            status TEXT DEFAULT 'draft',
            state_version INTEGER DEFAULT 1,
            last_state_change TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS executor_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            project_id INTEGER,
            status TEXT DEFAULT 'starting',
            FOREIGN KEY (project_id) REFERENCES projects(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_assignments (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id     TEXT NOT NULL UNIQUE,
            task_id           INTEGER NOT NULL,
            worker_id         TEXT NOT NULL,
            supervisor_run_id INTEGER,
            project_id        INTEGER NOT NULL,
            agent_type_required TEXT NOT NULL DEFAULT 'executor',
            decision_reason   TEXT DEFAULT '',
            priority          TEXT DEFAULT 'normal'
                              CHECK (priority IN ('low','normal','high','critical')),
            status            TEXT NOT NULL DEFAULT 'assigned'
                              CHECK (status IN ('assigned','acknowledged','running',
                                    'completed','failed','timeout','retrying','cancelled')),
            lease_token       TEXT,
            lease_expires_at  TEXT,
            retry_count       INTEGER DEFAULT 0,
            max_retries       INTEGER DEFAULT 2,
            idempotency_key   TEXT UNIQUE,
            dispatched_at     TEXT,
            acknowledged_at   TEXT,
            started_at        TEXT,
            completed_at      TEXT,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (task_id) REFERENCES development_tasks(id),
            FOREIGN KEY (project_id) REFERENCES projects(id),
            FOREIGN KEY (supervisor_run_id) REFERENCES executor_runs(id)
        )
    """)

    # Create OLD index (012 era, no 'timeout')
    conn.execute(f"""CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
        ON task_assignments(task_id)
        WHERE {OLD_WHERE}""")

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
        # ── Test 1: Basic migration applies ──
        print("\n[TEST 1] Migration 014 applies...")
        assert migrate(str(test_db)), "migration failed"

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row

        new_sql = _get_index_sql(conn, INDEX_NAME)
        check("index exists after migration", new_sql is not None)
        check("index excludes 'timeout'", _index_has_timeout(new_sql) if new_sql else False,
              f"SQL: {new_sql}")

        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        check("integrity_check = ok", cur.fetchone()[0] == "ok")

        cur.execute("PRAGMA foreign_key_check")
        check("foreign_key_check = 0", len(cur.fetchall()) == 0)
        conn.close()

        # ── Test 2: Idempotent re-run ──
        print("\n[TEST 2] Idempotent re-run...")
        assert migrate(str(test_db)), "idempotent re-run failed"
        check("idempotent re-run", True)

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row
        final_sql = _get_index_sql(conn, INDEX_NAME)
        check("index still excludes 'timeout' after re-run",
              _index_has_timeout(final_sql) if final_sql else False)
        conn.close()

        # ── Test 3: Rollback restores old index ──
        print("\n[TEST 3] Rollback restores 012-era index...")
        assert rollback(str(test_db)), "rollback failed"

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row
        rolled_sql = _get_index_sql(conn, INDEX_NAME)
        check("index exists after rollback", rolled_sql is not None)
        check("index does NOT exclude 'timeout' after rollback",
              not _index_has_timeout(rolled_sql) if rolled_sql else False,
              f"SQL: {rolled_sql}")

        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        check("integrity_check after rollback", cur.fetchone()[0] == "ok")
        cur.execute("PRAGMA foreign_key_check")
        check("foreign_key_check after rollback", len(cur.fetchall()) == 0)
        conn.close()

        # ── Test 4: Re-migrate after rollback ──
        print("\n[TEST 4] Re-migrate after rollback...")
        assert migrate(str(test_db)), "re-migration failed"

        conn = sqlite3.connect(str(test_db))
        conn.row_factory = sqlite3.Row
        remigrate_sql = _get_index_sql(conn, INDEX_NAME)
        check("index excludes 'timeout' after re-migration",
              _index_has_timeout(remigrate_sql) if remigrate_sql else False)
        conn.close()

        # ── Functional Tests on new index ──
        print("\n[FUNCTIONAL] Index behavior tests...")

        # Rebuild clean schema with NEW index
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_assignments")
        conn.execute("INSERT OR IGNORE INTO development_tasks (id, project_id, status, state_version) VALUES (1, 1, 'queued', 1)")
        conn.execute("INSERT OR IGNORE INTO development_tasks (id, project_id, status, state_version) VALUES (2, 1, 'queued', 1)")
        conn.commit()

        # ── Test A: timeout does NOT block new assignment ──
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status, lease_token, lease_expires_at)
            VALUES ('ta-tmo-1', 1, 'w-timeout', 1, 'executor',
                    'timeout', 'tok-old', datetime('now', '-1 hour'))
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-tmo-2', 1, 'w-new', 1, 'executor', 'assigned')
            """)
            conn.commit()
            check("timeout does NOT block new 'assigned' on same task", True)
        except sqlite3.IntegrityError:
            check("timeout does NOT block new 'assigned' on same task", False, "UNIQUE constraint blocked")

        conn.close()

        # ── Test B: assigned STILL blocks (under new index) ──
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_assignments")
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status)
            VALUES ('ta-b1', 1, 'w-a', 1, 'executor', 'assigned')
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-b2', 1, 'w-b', 1, 'executor', 'assigned')
            """)
            conn.commit()
            check("assigned STILL blocks second active assignment", False, "should have raised")
        except sqlite3.IntegrityError:
            check("assigned STILL blocks second active assignment", True)
        conn.close()

        # ── Test C: running STILL blocks ──
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_assignments")
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status)
            VALUES ('ta-c1', 2, 'w-r', 1, 'executor', 'running')
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-c2', 2, 'w-r2', 1, 'executor', 'assigned')
            """)
            conn.commit()
            check("running STILL blocks second active assignment", False, "should have raised")
        except sqlite3.IntegrityError:
            check("running STILL blocks second active assignment", True)
        conn.close()

        # ── Test D: retrying STILL blocks ──
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_assignments")
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status)
            VALUES ('ta-d1', 1, 'w-rt', 1, 'executor', 'retrying')
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-d2', 1, 'w-rt2', 1, 'executor', 'assigned')
            """)
            conn.commit()
            check("retrying STILL blocks second active assignment", False, "should have raised")
        except sqlite3.IntegrityError:
            check("retrying STILL blocks second active assignment", True)
        conn.close()

        # ── Test E: completed does NOT block ──
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_assignments")
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status)
            VALUES ('ta-e1', 1, 'w-comp', 1, 'executor', 'completed')
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-e2', 1, 'w-comp2', 1, 'executor', 'assigned')
            """)
            conn.commit()
            check("completed does NOT block new assignment", True)
        except sqlite3.IntegrityError:
            check("completed does NOT block new assignment", False, "UNIQUE constraint blocked")
        conn.close()

        # ── Test F: failed does NOT block ──
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_assignments")
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status)
            VALUES ('ta-f1', 1, 'w-fail', 1, 'executor', 'failed')
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-f2', 1, 'w-fail2', 1, 'executor', 'assigned')
            """)
            conn.commit()
            check("failed does NOT block new assignment", True)
        except sqlite3.IntegrityError:
            check("failed does NOT block new assignment", False, "UNIQUE constraint blocked")
        conn.close()

        # ── Test G: cancelled does NOT block ──
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_assignments")
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status)
            VALUES ('ta-g1', 1, 'w-cancel', 1, 'executor', 'cancelled')
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-g2', 1, 'w-cancel2', 1, 'executor', 'assigned')
            """)
            conn.commit()
            check("cancelled does NOT block new assignment", True)
        except sqlite3.IntegrityError:
            check("cancelled does NOT block new assignment", False, "UNIQUE constraint blocked")
        conn.close()

        # ── Test H: timeout does NOT block (re-verify clean) ──
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_assignments")
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status)
            VALUES ('ta-h1', 1, 'w-to', 1, 'executor', 'timeout')
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-h2', 1, 'w-to2', 1, 'executor', 'assigned')
            """)
            conn.commit()
            check("timeout does NOT block new assignment (clean verify)", True)
        except sqlite3.IntegrityError:
            check("timeout does NOT block new assignment (clean verify)", False, "UNIQUE constraint blocked")
        conn.close()

        # ── Test I: Rollback behavior — timeout blocks again under old index ──
        print("\n[TEST I] After rollback, timeout BLOCKS under old index...")
        assert rollback(str(test_db)), "rollback for Test I failed"

        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = OFF")

        # Verify old index back
        rb_sql = _get_index_sql(conn, INDEX_NAME)
        check("rollback restored old index (no timeout)",
              not _index_has_timeout(rb_sql) if rb_sql else False)

        conn.execute("DELETE FROM task_assignments")
        conn.execute("""
            INSERT INTO task_assignments
                (assignment_id, task_id, worker_id, project_id,
                 agent_type_required, status)
            VALUES ('ta-i1', 1, 'w-to', 1, 'executor', 'timeout')
        """)
        conn.commit()

        try:
            conn.execute("""
                INSERT INTO task_assignments
                    (assignment_id, task_id, worker_id, project_id,
                     agent_type_required, status)
                VALUES ('ta-i2', 1, 'w-to2', 1, 'executor', 'assigned')
            """)
            conn.commit()
            # Under old index, timeout might still block
            check("timeout BLOCKS under rollback-restored old index", False, "Should have blocked")
        except sqlite3.IntegrityError:
            check("timeout BLOCKS under rollback-restored old index", True)
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
    print(f"  MIGRATION 014 TEST RESULT: {passed} PASSED, {failed} FAILED")
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
            print("Usage: python -m app.migrations.014_task_assignment_timeout_index [test|rollback [--force]]")
    else:
        migrate()
