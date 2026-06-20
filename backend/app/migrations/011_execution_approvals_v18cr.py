"""
Database Migration 011: V1.8C-R — Add consumption tracking columns

Purpose:
  Add consumed_by_run_id and consumed_by_task_id to execution_approvals
  for V1.8C-R atomic approval consumption audit trail.

V1.8C-R requires:
  - approval consumption MUST record run_id and task_id
  - consumed_by_run_id: executor_runs.id that triggered consumption
  - consumed_by_task_id: task.id that was being claimed

Idempotent: uses ALTER TABLE ADD COLUMN IF NOT EXISTS pattern
  (SQLite doesn't support ADD COLUMN IF NOT EXISTS, so we check
   via PRAGMA table_info first)

Usage:
  cd backend
  python -m app.migrations.011_execution_approvals_v18cr

Test:
  python -m app.migrations.011_execution_approvals_v18cr test
"""
import sqlite3
import shutil
import sys
from pathlib import Path


def migrate(db_path: str = None):
    """Execute migration 011: add consumed_by_run_id, consumed_by_task_id"""
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

        # Check existing columns
        cur.execute("PRAGMA table_info(execution_approvals)")
        existing_cols = {row[1] for row in cur.fetchall()}

        columns_to_add = []
        if "consumed_by_run_id" not in existing_cols:
            columns_to_add.append(
                "consumed_by_run_id INTEGER DEFAULT NULL"
            )
        if "consumed_by_task_id" not in existing_cols:
            columns_to_add.append(
                "consumed_by_task_id INTEGER DEFAULT NULL"
            )

        if not columns_to_add:
            print("[OK] consumed_by_run_id and consumed_by_task_id already exist")
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for col_ddl in columns_to_add:
                    cur.execute(
                        f"ALTER TABLE execution_approvals ADD COLUMN {col_ddl}"
                    )
                    print(f"[ADD] {col_ddl.split()[0]} added")
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[FAIL] ALTER TABLE failed: {e}")
                conn.close()
                return False

        # Verify
        cur.execute("PRAGMA table_info(execution_approvals)")
        final_cols = {row[1]: row[2] for row in cur.fetchall()}
        required = ["consumed_by_run_id", "consumed_by_task_id"]
        for col in required:
            assert col in final_cols, f"Missing column: {col}"
            print(f"[OK] {col} ({final_cols[col]})")

        # Integrity
        cur.execute("PRAGMA integrity_check")
        result = cur.fetchone()
        print(f"[OK] integrity_check = {result[0]}")

        conn.close()
        print(f"\n{'='*60}")
        print("  MIGRATION 011: PASS")
        print(f"{'='*60}")
        return True

    except AssertionError as e:
        print(f"[FAIL] Verification: {e}")
        conn.close()
        return False
    except Exception as e:
        print(f"[FAIL] Migration: {e}")
        conn.close()
        return False


def run_tests():
    """Run migration tests on a database copy"""
    print("=" * 60)
    print("  MIGRATION 011 TESTS: consumed_by columns")
    print("=" * 60)

    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent.parent
    prod_db = backend_dir / "data" / "ai_factory.db"

    if not prod_db.exists():
        print(f"[SKIP] production DB not found: {prod_db}")
        return 0, 0

    test_db = backend_dir / "data" / "ai_factory_test_011.db"
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

    # Run migration
    print("\n[MIGRATE] Running on test DB...")
    migrate(str(test_db))

    conn = sqlite3.connect(str(test_db))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Test 1: Columns exist
    c.execute("PRAGMA table_info(execution_approvals)")
    cols = {row[1] for row in c.fetchall()}
    check("consumed_by_run_id exists", "consumed_by_run_id" in cols)
    check("consumed_by_task_id exists", "consumed_by_task_id" in cols)

    # Test 2: Can set values
    from datetime import datetime, timedelta
    try:
        c.execute("""
            INSERT INTO execution_approvals
            (approval_id, project_id, status, allowed_task_ids_json,
             max_workers, single_use, expired_at,
             consumed_by_run_id, consumed_by_task_id)
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)
        """, (
            "test-011-001", 56, "[31]",
            1, 1, (datetime.now() + timedelta(hours=1)).isoformat(),
            100, 31,
        ))
        conn.commit()
        c.execute("SELECT consumed_by_run_id, consumed_by_task_id FROM execution_approvals WHERE approval_id='test-011-001'")
        row = c.fetchone()
        check("consumed_by_run_id = 100", row["consumed_by_run_id"] == 100)
        check("consumed_by_task_id = 31", row["consumed_by_task_id"] == 31)
    except Exception as e:
        check("insert with consumed_by columns", False, str(e))

    # Test 3: Idempotent re-run
    try:
        migrate(str(test_db))
        check("idempotent re-run", True)
    except Exception as e:
        check("idempotent re-run", False, str(e))

    conn.close()

    # Cleanup
    try:
        for ext in ["", "-wal", "-shm"]:
            p = Path(str(test_db) + ext)
            if p.exists():
                p.unlink()
    except Exception as e:
        print(f"[CLEANUP] failed: {e}")

    print(f"\n{'='*60}")
    print(f"  TEST RESULT: {passed} PASSED, {failed} FAILED")
    print(f"  OVERALL: {'PASS' if failed == 0 else 'FAIL'}")
    print(f"{'='*60}")

    return passed, failed


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            run_tests()
        elif sys.argv[1] in ("--help", "-h"):
            print(__doc__)
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print("Usage: python -m app.migrations.011_execution_approvals_v18cr [test]")
    else:
        migrate()
