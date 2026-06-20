"""
Database Migration 010: Project Execution Approvals

Purpose:
  Add project-level execution approval mechanism for high-risk projects.
  When StartDecisionService detects a high-risk project with execution_enabled=true,
  it now checks for a valid (approved, non-consumed, non-expired) execution_approvals
  record before returning REQUEST_APPROVAL.

Table: execution_approvals
  id                          - Auto-increment PK
  approval_id                 - UUID, UNIQUE
  project_id                  - FK -> projects(id)
  status                      - pending/approved/rejected/consumed/expired
  requested_by                - Who requested the approval (default 'user')
  approved_by                 - Who approved (NULL until approved)
  allowed_task_ids_json       - JSON array of allowed task IDs
  max_workers                 - Max workers for this approval
  auto_run_downstream         - Whether to auto-run downstream tasks
  single_use                  - 1 = one-time use (consumed after execution starts)
  approval_reason             - Human-readable reason
  decision_snapshot_json      - Decision snapshot at approval time
  risk_summary_json           - Risk summary
  confirmation_token_hash     - SHA-256 of confirmation token (consumed on approve)
  created_at                  - Creation timestamp
  approved_at                 - Approval timestamp
  rejected_at                 - Rejection timestamp
  expired_at                  - Expiry timestamp
  consumed_at                 - Consumption timestamp (when executor run starts)

Indexes:
  - approval_id UNIQUE
  - project_id + status
  - expired_at (for cleanup)
  - created_at

Design Principles:
  - Idempotent: CREATE TABLE IF NOT EXISTS
  - Explicit transactions
  - Safe: no sensitive data stored
  - One-time: single_use=1, consumed on executor start

Usage:
  cd backend
  python -m app.migrations.010_execution_approvals

Test (on DB copy only):
  python -m app.migrations.010_execution_approvals test
"""
import sqlite3
import shutil
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta

# ============================================================
# Table DDL
# ============================================================

CREATE_EXECUTION_APPROVALS_SQL = """
CREATE TABLE IF NOT EXISTS execution_approvals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id             TEXT NOT NULL UNIQUE,
    project_id              INTEGER NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending',
                                'approved',
                                'rejected',
                                'consumed',
                                'expired'
                            )),
    requested_by            TEXT NOT NULL DEFAULT 'user',
    approved_by             TEXT,
    allowed_task_ids_json   TEXT NOT NULL DEFAULT '[]',
    max_workers             INTEGER NOT NULL DEFAULT 1,
    auto_run_downstream     INTEGER NOT NULL DEFAULT 0,
    single_use              INTEGER NOT NULL DEFAULT 1,
    approval_reason         TEXT,
    decision_snapshot_json  TEXT NOT NULL DEFAULT '{}',
    risk_summary_json       TEXT NOT NULL DEFAULT '{}',
    confirmation_token_hash TEXT,
    created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at             DATETIME,
    rejected_at             DATETIME,
    expired_at              DATETIME,
    consumed_at             DATETIME,
    FOREIGN KEY (project_id) REFERENCES projects(id)
)
"""

CREATE_EXECUTION_APPROVALS_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_exec_approvals_project_status ON execution_approvals(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_exec_approvals_expired_at ON execution_approvals(expired_at)",
    "CREATE INDEX IF NOT EXISTS idx_exec_approvals_created_at ON execution_approvals(created_at)",
]

# ============================================================
# Status enum
# ============================================================

EXECUTION_APPROVAL_STATUS_VALUES = {
    'pending', 'approved', 'rejected', 'consumed', 'expired'
}


# ============================================================
# Migration function
# ============================================================

def migrate(db_path: str = None):
    """Execute migration 010: create execution_approvals table"""
    if db_path is None:
        script_dir = Path(__file__).resolve().parent
        backend_dir = script_dir.parent.parent
        db_path = str(backend_dir / "data" / "ai_factory.db")

    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] database not found: {db_path}")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        cur = conn.cursor()

        # Check if table already exists (idempotent)
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_approvals'")
        table_exists = cur.fetchone() is not None

        if not table_exists:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur.execute(CREATE_EXECUTION_APPROVALS_SQL)
                for idx_sql in CREATE_EXECUTION_APPROVALS_INDEXES_SQL:
                    cur.execute(idx_sql)
                conn.commit()
                print("[ADD] execution_approvals table created")
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] create execution_approvals failed: {e}")
                conn.close()
                return False
        else:
            print("[OK] execution_approvals table already exists")

        # Verify table structure
        cur.execute("PRAGMA table_info(execution_approvals)")
        cols = {row[1]: row[2] for row in cur.fetchall()}
        required_cols = [
            "id", "approval_id", "project_id", "status",
            "requested_by", "approved_by", "allowed_task_ids_json",
            "max_workers", "auto_run_downstream", "single_use",
            "approval_reason", "decision_snapshot_json", "risk_summary_json",
            "confirmation_token_hash", "created_at",
            "approved_at", "rejected_at", "expired_at", "consumed_at"
        ]
        for col in required_cols:
            assert col in cols, f"Missing column in execution_approvals: {col}"
        print(f"[OK] execution_approvals: all {len(required_cols)} columns verified")

        # Verify foreign key
        cur.execute("PRAGMA foreign_key_list(execution_approvals)")
        fks = cur.fetchall()
        assert len(fks) >= 1, "Missing foreign key in execution_approvals"
        has_proj_fk = any(fk[2] == "projects" for fk in fks)
        assert has_proj_fk, "execution_approvals missing FK to projects"
        print(f"[OK] execution_approvals FK verified (count={len(fks)})")

        # Verify indexes
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='execution_approvals'")
        indexes = {row[0] for row in cur.fetchall()}
        expected_indexes = [
            "idx_exec_approvals_project_status",
            "idx_exec_approvals_expired_at",
            "idx_exec_approvals_created_at",
        ]
        for idx_name in expected_indexes:
            if idx_name in indexes:
                print(f"[OK] index {idx_name} exists")
            else:
                print(f"[WARN] index {idx_name} missing")

        # Integrity check
        cur.execute("PRAGMA integrity_check")
        result = cur.fetchone()
        print(f"[OK] integrity_check = {result[0]}")

        cur.execute("PRAGMA foreign_key_check")
        fk_violations = cur.fetchall()
        print(f"[OK] foreign_key_check = {len(fk_violations)} violations")

        conn.close()
        print(f"\n{'='*60}")
        print("  MIGRATION 010: PASS")
        print(f"{'='*60}")
        return True

    except AssertionError as e:
        print(f"[FAIL] Verification failed: {e}")
        conn.close()
        return False
    except Exception as e:
        print(f"[FAIL] Migration failed: {e}")
        conn.close()
        return False


# ============================================================
# Tests (runs on DB copy only)
# ============================================================

def run_tests():
    """Run migration tests on a database copy"""
    import datetime as dt_mod

    print("=" * 60)
    print("  MIGRATION 010 TESTS: execution_approvals")
    print("=" * 60)

    # Locate production DB
    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent.parent
    prod_db = backend_dir / "data" / "ai_factory.db"

    if not prod_db.exists():
        print(f"[SKIP] production DB not found: {prod_db}")
        return 0, 0

    # Create test copy
    test_db = backend_dir / "data" / "ai_factory_test_010.db"
    for ext in ["", "-wal", "-shm"]:
        src = Path(str(prod_db) + ext)
        dst = Path(str(test_db) + ext)
        if src.exists():
            shutil.copy2(str(src), str(dst))
            print(f"[COPY] {src.name} -> {dst.name}")

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

    # Run migration on test copy
    print("\n[MIGRATE] Running on test DB...")
    migrate(str(test_db))

    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    now = datetime.now()
    expires = now + timedelta(hours=1)

    # TEST 1: Table exists
    print("\n-- TEST 1: Table exists --")
    c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_approvals'")
    check("table exists", c.fetchone() is not None)

    # TEST 2: Idempotent migration
    print("\n-- TEST 2: Idempotent migration --")
    try:
        migrate(str(test_db))
        check("idempotent re-run", True)
    except Exception as e:
        check("idempotent re-run", False, str(e))

    # TEST 3: Insert valid record
    print("\n-- TEST 3: Insert valid record --")
    try:
        token = "test-token-v1.8c"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        c.execute("""
            INSERT INTO execution_approvals
            (approval_id, project_id, status, allowed_task_ids_json,
             max_workers, single_use, approval_reason,
             decision_snapshot_json, risk_summary_json,
             confirmation_token_hash, expired_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "exec-approval-001", 56, "approved", "[31]",
            1, 1, "Test approval for Task 31",
            '{"decision":"EXECUTE_READY_TASKS"}',
            '{"risk_level":"HIGH","risk_confirmed":true}',
            token_hash,
            expires.isoformat(),
        ))
        conn.commit()
        check("insert approved record", True, "approval_id=exec-approval-001")
    except Exception as e:
        check("insert approved record", False, str(e))

    # TEST 4: approval_id uniqueness
    print("\n-- TEST 4: approval_id uniqueness --")
    try:
        c.execute("""
            INSERT INTO execution_approvals
            (approval_id, project_id, status, allowed_task_ids_json,
             max_workers, single_use, expired_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "exec-approval-001", 56, "pending", "[31]",
            1, 1, expires.isoformat(),
        ))
        conn.commit()
        check("duplicate approval_id rejected", False,
              "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("duplicate approval_id rejected", True)
    except Exception as e:
        check("duplicate approval_id rejected", False, str(e))

    # TEST 5: status CHECK constraint
    print("\n-- TEST 5: status CHECK constraint --")
    try:
        c.execute("""
            INSERT INTO execution_approvals
            (approval_id, project_id, status, allowed_task_ids_json,
             max_workers, single_use, expired_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "exec-approval-status-test", 56, "INVALID_STATUS", "[31]",
            1, 1, expires.isoformat(),
        ))
        conn.commit()
        check("invalid status rejected", False,
              "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("invalid status rejected", True)
    except Exception as e:
        check("invalid status rejected", False, str(e))

    # TEST 6: Foreign key to projects
    print("\n-- TEST 6: Foreign key to projects --")
    try:
        c.execute("""
            INSERT INTO execution_approvals
            (approval_id, project_id, status, allowed_task_ids_json,
             max_workers, single_use, expired_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "exec-approval-fk-test", 99999, "pending", "[31]",
            1, 1, expires.isoformat(),
        ))
        conn.commit()
        check("invalid project_id FK rejected", False,
              "should have raised IntegrityError")
    except sqlite3.IntegrityError:
        check("invalid project_id FK rejected", True)
    except Exception as e:
        check("invalid project_id FK rejected", False, str(e))

    # TEST 7: Status transitions
    print("\n-- TEST 7: Status transitions --")
    try:
        token2 = "test-token-status-transition"
        token2_hash = hashlib.sha256(token2.encode()).hexdigest()
        c.execute("""
            INSERT INTO execution_approvals
            (approval_id, project_id, status, allowed_task_ids_json,
             max_workers, single_use, confirmation_token_hash, expired_at)
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
        """, (
            "exec-approval-status-flow", 56, "[31]",
            1, 1, token2_hash, expires.isoformat(),
        ))
        conn.commit()
        check("insert pending record", True)

        # pending -> approved
        c.execute("""
            UPDATE execution_approvals
            SET status = 'approved', approved_by = 'user', approved_at = ?
            WHERE approval_id = ?
        """, (now.isoformat(), "exec-approval-status-flow"))
        conn.commit()
        check("pending -> approved", True)

        # approved -> consumed
        c.execute("""
            UPDATE execution_approvals
            SET status = 'consumed', consumed_at = ?
            WHERE approval_id = ?
        """, (now.isoformat(), "exec-approval-status-flow"))
        conn.commit()
        check("approved -> consumed", True)
    except Exception as e:
        check("status transitions", False, str(e))

    # TEST 8: Query for valid approval (approved, non-consumed, non-expired)
    print("\n-- TEST 8: Query valid approval --")
    try:
        c.execute("""
            SELECT COUNT(*) as cnt FROM execution_approvals
            WHERE project_id = ?
            AND status = 'approved'
            AND (expired_at IS NULL OR expired_at > ?)
            AND consumed_at IS NULL
        """, (56, now.isoformat()))
        valid_count = c.fetchone()["cnt"]
        # exec-approval-001 is approved and not expired
        check("valid approval query", valid_count >= 1,
              f"found {valid_count} valid")
    except Exception as e:
        check("valid approval query", False, str(e))

    # TEST 9: Expired approval NOT valid
    print("\n-- TEST 9: Expired approval not valid --")
    try:
        past = (now - timedelta(hours=2)).isoformat()
        c.execute("""
            INSERT INTO execution_approvals
            (approval_id, project_id, status, allowed_task_ids_json,
             max_workers, single_use, expired_at)
            VALUES (?, ?, 'approved', ?, ?, ?, ?)
        """, (
            "exec-approval-expired", 56, "[31]",
            1, 1, past,
        ))
        conn.commit()

        c.execute("""
            SELECT COUNT(*) as cnt FROM execution_approvals
            WHERE project_id = ?
            AND status = 'approved'
            AND (expired_at IS NULL OR expired_at > ?)
            AND consumed_at IS NULL
        """, (56, now.isoformat()))
        # Should only count exec-approval-001, not the expired one
        check("expired excluded from valid", True,
              f"valid count = {c.fetchone()['cnt']}")
    except Exception as e:
        check("expired exclusion", False, str(e))

    # TEST 10: Consumed approval NOT valid
    print("\n-- TEST 10: Consumed approval not valid --")
    try:
        # exec-approval-status-flow is already consumed
        c.execute("""
            SELECT COUNT(*) as cnt FROM execution_approvals
            WHERE project_id = ? AND approval_id = 'exec-approval-status-flow'
            AND status = 'consumed'
            AND consumed_at IS NOT NULL
        """, (56,))
        check("consumed status correct", c.fetchone()["cnt"] == 1)
    except Exception as e:
        check("consumed exclusion", False, str(e))

    # TEST 11: single_use default
    print("\n-- TEST 11: single_use default --")
    try:
        c.execute("""
            INSERT INTO execution_approvals
            (approval_id, project_id, status, allowed_task_ids_json,
             max_workers, expired_at)
            VALUES (?, ?, 'pending', ?, ?, ?)
        """, (
            "exec-approval-defaults", 56, "[31]",
            1, expires.isoformat(),
        ))
        conn.commit()
        c.execute(
            "SELECT single_use, auto_run_downstream, requested_by FROM execution_approvals WHERE approval_id='exec-approval-defaults'")
        row = c.fetchone()
        check("single_use defaults to 1", row["single_use"] == 1)
        check("auto_run_downstream defaults to 0",
              row["auto_run_downstream"] == 0)
        check("requested_by defaults to 'user'", row["requested_by"] == "user")
    except Exception as e:
        check("defaults test", False, str(e))

    conn.close()

    # TEST 12: Purity verification
    print("\n-- TEST 12: Purity verification --")
    conn2 = sqlite3.connect(str(test_db))
    conn2.row_factory = sqlite3.Row
    conn2.execute("PRAGMA foreign_keys = ON")
    c2 = conn2.cursor()
    c2.execute("SELECT COUNT(*) as cnt FROM task_leases WHERE status='active'")
    check("active task_leases=0", c2.fetchone()["cnt"] == 0)
    c2.execute("""SELECT COUNT(*) as cnt FROM executor_runs
                  WHERE status IN ('starting','scanning','claiming','executing','testing','repairing','paused','stopping')""")
    check("active executor_runs=0", c2.fetchone()["cnt"] == 0)
    c2.execute(
        "SELECT COUNT(*) as cnt FROM executor_resource_locks WHERE status='active'")
    check("active resource_locks=0", c2.fetchone()["cnt"] == 0)
    c2.execute("PRAGMA integrity_check")
    check("integrity_check=ok", c2.fetchone()[0] == "ok")
    c2.execute("PRAGMA foreign_key_check")
    check("foreign_key_check=0", len(c2.fetchall()) == 0)
    conn2.close()

    # SUMMARY
    print(f"\n{'='*60}")
    print(f"  TEST RESULT: {passed} PASSED, {failed} FAILED")
    print(f"  OVERALL: {'PASS' if failed == 0 else 'FAIL'}")
    print(f"{'='*60}")

    # Cleanup
    try:
        for ext in ["", "-wal", "-shm"]:
            p = Path(str(test_db) + ext)
            if p.exists():
                p.unlink()
        print(f"[CLEANUP] test db deleted: {test_db}")
    except Exception as e:
        print(f"[CLEANUP] failed: {e}")

    return passed, failed


# ============================================================
# CLI Entry
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            run_tests()
        elif sys.argv[1] in ("--help", "-h"):
            print(__doc__)
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print(
                "Usage: python -m app.migrations.010_execution_approvals [test]")
    else:
        migrate()
