"""V2.0-B2b-R: Timeout Assignment Index 专项 pytest 测试

所有测试使用独立临时 SQLite 数据库，不接触正式数据库。

测试覆盖:
  1.  旧索引下 timeout assignment 阻止新 assignment
  2.  Migration 014 后 timeout 不再阻止
  3.  assigned 仍然阻止第二个 active assignment
  4.  running 仍然阻止
  5.  retrying 仍然阻止
  6.  completed 不阻止
  7.  failed 不阻止
  8.  cancelled 不阻止
  9.  timeout 不阻止
  10. migration 重复执行幂等
  11. rollback 恢复旧索引
  12. rollback 后 timeout 再次被旧索引阻止
  13. integrity_check=ok
  14. foreign_key_check=0
"""

import importlib
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import migration 014 module (digit-prefixed name requires importlib)
_migration = importlib.import_module("app.migrations.014_task_assignment_timeout_index")

INDEX_NAME = _migration.INDEX_NAME
OLD_WHERE = _migration.OLD_WHERE
NEW_WHERE = _migration.NEW_WHERE
migrate = _migration.migrate
rollback = _migration.rollback
_get_index_sql = _migration._get_index_sql
_index_has_timeout = _migration._index_has_timeout


# ── Fixtures ──

def _make_temp_db() -> str:
    """Create a temp database file path."""
    tmp_dir = tempfile.mkdtemp(prefix="test_014_")
    return os.path.join(tmp_dir, "test.db")


def _build_schema_with_old_index(db_path: str):
    """Build a post-012 schema with the OLD index (no 'timeout')."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")

    conn.execute("CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS development_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL, title TEXT DEFAULT '',
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT NOT NULL UNIQUE,
            task_id INTEGER NOT NULL, worker_id TEXT NOT NULL,
            supervisor_run_id INTEGER, project_id INTEGER NOT NULL,
            agent_type_required TEXT NOT NULL DEFAULT 'executor',
            decision_reason TEXT DEFAULT '', priority TEXT DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'assigned'
                CHECK (status IN ('assigned','acknowledged','running',
                      'completed','failed','timeout','retrying','cancelled')),
            lease_token TEXT, lease_expires_at TEXT,
            retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 2,
            idempotency_key TEXT UNIQUE,
            dispatched_at TEXT, acknowledged_at TEXT, started_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (task_id) REFERENCES development_tasks(id),
            FOREIGN KEY (project_id) REFERENCES projects(id),
            FOREIGN KEY (supervisor_run_id) REFERENCES executor_runs(id)
        )
    """)
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} ON task_assignments(task_id) WHERE {OLD_WHERE}")
    conn.execute("INSERT INTO projects (id, name) VALUES (1, 'test-project')")
    conn.execute("INSERT INTO development_tasks (id, project_id, status, state_version) VALUES (1, 1, 'queued', 1)")
    conn.execute("INSERT INTO development_tasks (id, project_id, status, state_version) VALUES (2, 1, 'queued', 1)")
    conn.commit()
    conn.close()


def _cleanup_temp_db(db_path: str):
    tmp_dir = os.path.dirname(db_path)
    for ext in ["", "-wal", "-shm"]:
        p = db_path + ext
        if os.path.exists(p):
            os.unlink(p)
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass


def _insert_assignment(conn, aid, tid, wid, st):
    conn.execute(
        "INSERT INTO task_assignments (assignment_id, task_id, worker_id, project_id, agent_type_required, status) "
        "VALUES (?, ?, ?, 1, 'executor', ?)",
        (aid, tid, wid, st))
    conn.commit()


def _can_insert(conn, aid, tid, wid, st) -> bool:
    try:
        _insert_assignment(conn, aid, tid, wid, st)
        return True
    except sqlite3.IntegrityError:
        return False


# ── Tests ──

class TestOldIndexBlocksTimeout:
    """1. 旧索引下 timeout 阻止新 assignment."""

    def test_timeout_blocks_under_old_index(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            conn = sqlite3.connect(db)
            sql = _get_index_sql(conn, INDEX_NAME)
            assert sql and not _index_has_timeout(sql), f"Expected old index, got: {sql}"
            _insert_assignment(conn, "a1", 1, "w1", "timeout")
            assert not _can_insert(conn, "a2", 1, "w2", "assigned"), \
                "Old index should block new assignment when timeout exists"
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestNewIndexAllowsTimeout:
    """2. Migration 014 后 timeout 不再阻止."""

    def test_timeout_allows_after_migration(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            sql = _get_index_sql(conn, INDEX_NAME)
            assert sql and _index_has_timeout(sql), f"Expected new index, got: {sql}"
            _insert_assignment(conn, "a1", 1, "w1", "timeout")
            assert _can_insert(conn, "a2", 1, "w2", "assigned"), \
                "New index should allow new assignment when timeout exists"
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestAssignedStillBlocks:
    """3. assigned 仍然阻止第二个 active assignment."""

    def test_assigned_blocks_second(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            _insert_assignment(conn, "a1", 1, "w1", "assigned")
            assert not _can_insert(conn, "a2", 1, "w2", "assigned")
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestRunningStillBlocks:
    """4. running 仍然阻止."""

    def test_running_blocks_second(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            _insert_assignment(conn, "a1", 1, "w1", "running")
            assert not _can_insert(conn, "a2", 1, "w2", "assigned")
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestRetryingStillBlocks:
    """5. retrying 仍然阻止."""

    def test_retrying_blocks_second(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            _insert_assignment(conn, "a1", 1, "w1", "retrying")
            assert not _can_insert(conn, "a2", 1, "w2", "assigned")
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestCompletedDoesNotBlock:
    """6. completed 不阻止."""

    def test_completed_allows_new(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            _insert_assignment(conn, "a1", 1, "w1", "completed")
            assert _can_insert(conn, "a2", 1, "w2", "assigned")
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestFailedDoesNotBlock:
    """7. failed 不阻止."""

    def test_failed_allows_new(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            _insert_assignment(conn, "a1", 1, "w1", "failed")
            assert _can_insert(conn, "a2", 1, "w2", "assigned")
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestCancelledDoesNotBlock:
    """8. cancelled 不阻止."""

    def test_cancelled_allows_new(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            _insert_assignment(conn, "a1", 1, "w1", "cancelled")
            assert _can_insert(conn, "a2", 1, "w2", "assigned")
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestTimeoutDoesNotBlock:
    """9. timeout 不阻止."""

    def test_timeout_allows_new(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            _insert_assignment(conn, "a1", 1, "w1", "timeout")
            assert _can_insert(conn, "a2", 1, "w2", "assigned")
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestIdempotentMigration:
    """10. Migration 014 重复执行幂等."""

    def test_migration_idempotent(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            c1 = sqlite3.connect(db)
            first = _get_index_sql(c1, INDEX_NAME)
            c1.close()
            assert migrate(db)
            c2 = sqlite3.connect(db)
            second = _get_index_sql(c2, INDEX_NAME)
            c2.close()
            assert first == second, "Index SQL should be identical after idempotent re-run"
            assert migrate(db)
            c3 = sqlite3.connect(db)
            third = _get_index_sql(c3, INDEX_NAME)
            c3.close()
            assert second == third
        finally:
            _cleanup_temp_db(db)


class TestRollback:
    """11. Rollback 恢复旧索引."""

    def test_rollback_restores_old_index(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            before = _get_index_sql(conn, INDEX_NAME)
            assert _index_has_timeout(before)
            conn.close()

            assert rollback(db)
            conn = sqlite3.connect(db)
            after = _get_index_sql(conn, INDEX_NAME)
            assert after and not _index_has_timeout(after), \
                f"Rollback should restore old index, got: {after}"
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestRollbackBlocksTimeoutAgain:
    """12. Rollback 后 timeout 再次被旧索引阻止."""

    def test_rollback_blocks_timeout_again(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            # Use separate task_ids so old index recreation doesn't conflict
            _insert_assignment(conn, "a1", 1, "w1", "timeout")
            before = _can_insert(conn, "a2", 1, "w2", "assigned")
            assert before, "New index should allow claim on same task when timeout exists"
            # Clean all assignments — required for rollback to recreate old index
            conn.execute("DELETE FROM task_assignments")
            conn.commit()
            conn.close()

            assert rollback(db)

            # Now test: under old index, timeout blocks new assignment
            conn = sqlite3.connect(db)
            _insert_assignment(conn, "a3", 1, "w3", "timeout")
            after = _can_insert(conn, "a4", 1, "w4", "assigned")
            assert not after, "After rollback, timeout should block again under old index"
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestIntegrityCheck:
    """13. integrity_check=ok."""

    def test_integrity_after_migration(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            assert cur.fetchone()[0] == "ok"
            conn.close()
        finally:
            _cleanup_temp_db(db)

    def test_integrity_after_rollback(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            assert rollback(db)
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check")
            assert cur.fetchone()[0] == "ok"
            conn.close()
        finally:
            _cleanup_temp_db(db)


class TestForeignKeyCheck:
    """14. foreign_key_check=0."""

    def test_fk_check_after_migration(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_key_check")
            assert len(cur.fetchall()) == 0
            conn.close()
        finally:
            _cleanup_temp_db(db)

    def test_fk_check_after_rollback(self):
        db = _make_temp_db()
        try:
            _build_schema_with_old_index(db)
            assert migrate(db)
            assert rollback(db)
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_key_check")
            assert len(cur.fetchall()) == 0
            conn.close()
        finally:
            _cleanup_temp_db(db)
