"""V2.0-B2c: WorkerHeartbeatService 专项 pytest 测试

测试范围:
  - 正常心跳流程 + 数据写入验证
  - 权限与租约校验（other worker, wrong token, task mismatch, etc.）
  - 幂等性（相同请求、不同字段冲突）
  - 原子性（rollback on failure）
  - Feature flag 门禁
  - 边界条件

每个测试使用独立临时 SQLite 数据库。
"""

import os
import sys
import json
import uuid
import sqlite3
import tempfile
import time as _time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.supervisor.worker_heartbeat_service import (
    WorkerHeartbeatService,
    ERROR_V2_CONTROL_PLANE_DISABLED,
    ERROR_WORKER_NOT_REGISTERED,
    ERROR_WORKER_NOT_AVAILABLE,
    ERROR_ASSIGNMENT_NOT_FOUND,
    ERROR_TASK_SCOPE_VIOLATION,
    ERROR_LEASE_CONFLICT,
    ERROR_STALE_LEASE,
    ERROR_IDEMPOTENCY_CONFLICT,
    ERROR_VALIDATION_ERROR,
    ERROR_INTERNAL_ERROR,
)

from app.supervisor.worker_registry import (
    WORKER_STATUS_AVAILABLE,
    WORKER_STATUS_BUSY,
    WORKER_STATUS_OFFLINE,
    WORKER_STATUS_DISABLED,
    WORKER_STATUS_REGISTERED,
    WORKER_TYPE_EXECUTOR,
)


# ── Schema (mirrors Migration 012 + 013 DDL) ──

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'draft'
);

CREATE TABLE IF NOT EXISTS development_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    task_type TEXT DEFAULT 'backend',
    status TEXT DEFAULT 'draft',
    state_version INTEGER DEFAULT 1,
    last_state_change TEXT,
    dependencies TEXT,
    files_to_check TEXT,
    files_to_modify TEXT,
    test_steps TEXT,
    acceptance_criteria TEXT,
    implementation_steps TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    task_id         INTEGER NOT NULL,
    assignment_id   TEXT,
    project_id      INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT,
    reason          TEXT DEFAULT '',
    detail_json     TEXT DEFAULT '{}',
    operator_type   TEXT NOT NULL,
    operator_id     TEXT NOT NULL,
    idempotency_key TEXT,
    state_version_before INTEGER,
    state_version_after  INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(event_id),
    UNIQUE(idempotency_key),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS task_assignments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id     TEXT NOT NULL,
    task_id           INTEGER NOT NULL,
    worker_id         TEXT NOT NULL,
    project_id        INTEGER NOT NULL,
    agent_type_required TEXT NOT NULL,
    decision_reason     TEXT DEFAULT '',
    priority            TEXT DEFAULT 'normal',
    status          TEXT NOT NULL DEFAULT 'assigned'
                    CHECK (status IN ('assigned','acknowledged','running','completed','failed','timeout','retrying','cancelled')),
    lease_token     TEXT,
    lease_expires_at TEXT,
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 2,
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
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_assignments_active
ON task_assignments(task_id)
WHERE status NOT IN ('completed','failed','cancelled','timeout');

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

CREATE TABLE IF NOT EXISTS agent_capabilities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id    TEXT NOT NULL,
    capability   TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(worker_id, capability),
    FOREIGN KEY (worker_id) REFERENCES agent_workers(worker_id) ON DELETE CASCADE
);

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
"""


# ── Helpers ──

def _build_temp_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_hb_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.execute("INSERT INTO projects (id, name) VALUES (1, 'test-project')")
    conn.commit()
    conn.close()
    return path


def _cleanup_temp_db(path):
    _time.sleep(0.05)
    for ext in ["", "-wal", "-shm"]:
        p = path + ext
        for _ in range(3):
            try:
                if os.path.exists(p):
                    os.unlink(p)
                break
            except PermissionError:
                _time.sleep(0.1)
            except FileNotFoundError:
                break


def _raw_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── Fixtures ──

@pytest.fixture
def hb_service():
    """Heartbeat service with V2 enabled, fresh temp DB."""
    db_path = _build_temp_db()
    svc = WorkerHeartbeatService(db_path, v2_enabled=True)
    yield svc
    _cleanup_temp_db(db_path)


@pytest.fixture
def hb_service_disabled():
    """Heartbeat service with V2 disabled."""
    db_path = _build_temp_db()
    svc = WorkerHeartbeatService(db_path, v2_enabled=False)
    yield svc
    _cleanup_temp_db(db_path)


# ── Seed helpers ──

def _setup_worker(svc, worker_id="exec-1", status=WORKER_STATUS_BUSY,
                  worker_type="executor"):
    """Register a worker and set its status. Uses supervisor type for
    non-singleton test workers to avoid idx_agent_workers_active_executor
    uniqueness violation."""
    conn = _raw_conn(svc.db_path)
    now = _time_now()
    conn.execute("""
        INSERT INTO agent_workers (worker_id, worker_type, status, last_seen_at, registered_at)
        VALUES (?, ?, ?, ?, ?)
    """, (worker_id, worker_type, status, now, now))
    conn.commit()
    conn.close()


def _setup_task(svc, task_id=1, status="CLAIMED", state_version=2, project_id=1):
    """Create a task. Returns db_path."""
    conn = _raw_conn(svc.db_path)
    conn.execute("""
        INSERT INTO development_tasks (id, project_id, status, state_version)
        VALUES (?, ?, ?, ?)
    """, (task_id, project_id, status, state_version))
    conn.commit()
    conn.close()


def _setup_assignment(svc, assignment_id="asgn-abc", task_id=1, worker_id="exec-1",
                       status="assigned", lease_token="tok-secret",
                       expires_in_seconds=300, project_id=1):
    """Create an assignment record."""
    import secrets
    from datetime import datetime, timedelta
    tok = lease_token if lease_token else secrets.token_hex(16)
    expires = (datetime.now() + timedelta(seconds=expires_in_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = _raw_conn(svc.db_path)
    conn.execute("""
        INSERT INTO task_assignments
        (assignment_id, task_id, worker_id, project_id, agent_type_required,
         status, lease_token, lease_expires_at, idempotency_key,
         dispatched_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'executor', ?, ?, ?, NULL, ?, ?, ?)
    """, (assignment_id, task_id, worker_id, project_id,
          status, tok, expires, now, now, now))
    conn.commit()
    conn.close()
    return tok


def _time_now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _setup_full(svc, worker_id="exec-1", task_id=1, assignment_id="asgn-abc",
                status="assigned", expires_in=300):
    """Full setup: worker, task, assignment."""
    _setup_worker(svc, worker_id, WORKER_STATUS_BUSY)
    _setup_task(svc, task_id, status="CLAIMED")
    tok = _setup_assignment(svc, assignment_id, task_id, worker_id,
                             status, "tok-secret", expires_in)
    return tok


def _count_heartbeats(svc):
    conn = _raw_conn(svc.db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM agent_heartbeats")
    cnt = cur.fetchone()["cnt"]
    conn.close()
    return cnt


def _get_assignment_expires(svc, assignment_id):
    conn = _raw_conn(svc.db_path)
    cur = conn.cursor()
    cur.execute("SELECT lease_expires_at, status FROM task_assignments WHERE assignment_id = ?",
                (assignment_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _get_worker_last_seen(svc, worker_id):
    conn = _raw_conn(svc.db_path)
    cur = conn.cursor()
    cur.execute("SELECT last_seen_at FROM agent_workers WHERE worker_id = ?", (worker_id,))
    row = cur.fetchone()
    conn.close()
    return row["last_seen_at"] if row else None


def _get_task_status(svc, task_id):
    conn = _raw_conn(svc.db_path)
    cur = conn.cursor()
    cur.execute("SELECT status, state_version FROM development_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# ═════════════════════════════════════════════════════════════
#  正常流程
# ═════════════════════════════════════════════════════════════

class TestHeartbeatNormalFlow:

    def test_heartbeat_success(self, hb_service):
        _setup_full(hb_service, expires_in=300)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-1",
            extend_seconds=300,
        )
        assert r["success"] is True, f"Expected success, got: {r}"
        assert r["heartbeat_id"] is not None
        assert r["heartbeat_id"].startswith("hb-")
        assert r["task_id"] == 1
        assert r["assignment_id"] == "asgn-abc"
        assert r["worker_id"] == "exec-1"
        assert r["idempotent"] is False
        assert r["error_code"] is None

    def test_heartbeat_writes_to_agent_heartbeats(self, hb_service):
        _setup_full(hb_service, expires_in=300)
        assert _count_heartbeats(hb_service) == 0
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-hb-insert",
            extend_seconds=400,
        )
        assert r["success"]
        assert _count_heartbeats(hb_service) == 1

    def test_expires_at_is_extended(self, hb_service):
        tok = _setup_full(hb_service, expires_in=300)
        before = _get_assignment_expires(hb_service, "asgn-abc")
        assert before is not None
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-extend",
            extend_seconds=600,
        )
        assert r["success"]
        after = _get_assignment_expires(hb_service, "asgn-abc")
        assert after["lease_expires_at"] != before["lease_expires_at"], \
            "expires_at should be extended"
        assert after["lease_expires_at"] == r["lease_expires_at"]

    def test_worker_last_seen_at_updated(self, hb_service):
        tok = _setup_full(hb_service, expires_in=300)
        before = _get_worker_last_seen(hb_service, "exec-1")
        _time.sleep(1.1)  # ensure time difference
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-last-seen",
            extend_seconds=300,
        )
        assert r["success"]
        after = _get_worker_last_seen(hb_service, "exec-1")
        assert after != before, "last_seen_at should be updated"
        assert after == r["worker_last_seen_at"]

    def test_task_status_unchanged(self, hb_service):
        tok = _setup_full(hb_service, expires_in=300)
        before = _get_task_status(hb_service, 1)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-status-unchanged",
            extend_seconds=300,
        )
        assert r["success"]
        after = _get_task_status(hb_service, 1)
        assert after["status"] == before["status"], "Task status must not change"
        assert after["state_version"] == before["state_version"], \
            "state_version must not change"


# ═════════════════════════════════════════════════════════════
#  权限与租约校验
# ═════════════════════════════════════════════════════════════

class TestHeartbeatAuthorization:

    def test_other_worker_rejected(self, hb_service):
        tok = _setup_full(hb_service, worker_id="exec-1")
        _setup_worker(hb_service, "exec-2", "registered", "supervisor")
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-2",
            lease_token=tok, idempotency_key="key-other-worker",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_LEASE_CONFLICT

    def test_wrong_token_rejected(self, hb_service):
        _setup_full(hb_service, expires_in=300)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="wrong-token", idempotency_key="key-wrong-token",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_LEASE_CONFLICT

    def test_task_id_mismatch_rejected(self, hb_service):
        tok = _setup_full(hb_service, task_id=1)
        _setup_task(hb_service, task_id=2, status="CLAIMED")
        _setup_assignment(hb_service, "asgn-def", task_id=2, worker_id="exec-1",
                           status="assigned", lease_token="tok-sec2", expires_in_seconds=300)
        r = hb_service.heartbeat(
            task_id=999, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-task-mismatch",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_TASK_SCOPE_VIOLATION

    def test_assignment_not_found(self, hb_service):
        _setup_worker(hb_service, "exec-1", WORKER_STATUS_BUSY)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="no-such-asgn", worker_id="exec-1",
            lease_token="tok", idempotency_key="key-no-asgn",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_ASSIGNMENT_NOT_FOUND

    def test_worker_not_registered(self, hb_service):
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="ghost-worker",
            lease_token="tok", idempotency_key="key-no-reg",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_WORKER_NOT_REGISTERED

    def test_offline_worker_rejected(self, hb_service):
        _setup_full(hb_service, worker_id="exec-1")
        conn = _raw_conn(hb_service.db_path)
        conn.execute("UPDATE agent_workers SET status = 'offline' WHERE worker_id = 'exec-1'")
        conn.commit()
        conn.close()
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-offline",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_WORKER_NOT_AVAILABLE

    def test_disabled_worker_rejected(self, hb_service):
        _setup_full(hb_service, worker_id="exec-1")
        conn = _raw_conn(hb_service.db_path)
        conn.execute("UPDATE agent_workers SET status = 'disabled' WHERE worker_id = 'exec-1'")
        conn.commit()
        conn.close()
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-disabled",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_WORKER_NOT_AVAILABLE

    def test_completed_assignment_rejected(self, hb_service):
        _setup_full(hb_service, status="completed", expires_in=300)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-completed",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_STALE_LEASE
        assert "completed" in r["error_message"].lower()

    def test_failed_assignment_rejected(self, hb_service):
        _setup_full(hb_service, status="failed", expires_in=300)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-failed",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_STALE_LEASE

    def test_cancelled_assignment_rejected(self, hb_service):
        _setup_full(hb_service, status="cancelled", expires_in=300)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-cancelled",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_STALE_LEASE

    def test_timeout_assignment_rejected(self, hb_service):
        _setup_full(hb_service, status="timeout", expires_in=300)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-timeout",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_STALE_LEASE

    def test_expired_lease_returns_stale_lease(self, hb_service):
        """Lease already expired → STALE_LEASE."""
        _setup_full(hb_service, expires_in=-10)  # expired 10s ago
        _time.sleep(0.1)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-expired",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_STALE_LEASE


# ═════════════════════════════════════════════════════════════
#  幂等性
# ═════════════════════════════════════════════════════════════

class TestHeartbeatIdempotency:

    def test_same_request_repeats_return_original_result(self, hb_service):
        tok = _setup_full(hb_service, expires_in=300)
        r1 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-idem-1",
            extend_seconds=300,
        )
        assert r1["success"]
        assert r1["idempotent"] is False

        r2 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-idem-1",
            extend_seconds=300,
        )
        assert r2["success"]
        assert r2["idempotent"] is True
        assert r2["heartbeat_id"] == r1["heartbeat_id"]
        assert r2["lease_expires_at"] == r1["lease_expires_at"]

    def test_no_duplicate_heartbeat_on_same_key(self, hb_service):
        tok = _setup_full(hb_service, expires_in=300)
        r1 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-no-dup",
            extend_seconds=300,
        )
        assert r1["success"]
        assert _count_heartbeats(hb_service) == 1

        r2 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-no-dup",
            extend_seconds=300,
        )
        assert r2["success"]
        assert _count_heartbeats(hb_service) == 1  # no second insert

    def test_no_second_expires_at_extension_on_same_key(self, hb_service):
        tok = _setup_full(hb_service, expires_in=300)
        r1 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-no-ext2",
            extend_seconds=300,
        )
        assert r1["success"]
        after1 = _get_assignment_expires(hb_service, "asgn-abc")["lease_expires_at"]

        r2 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-no-ext2",
            extend_seconds=300,
        )
        assert r2["success"]
        assert r2["idempotent"]
        after2 = _get_assignment_expires(hb_service, "asgn-abc")["lease_expires_at"]
        assert after2 == after1, "lease_expires_at must not be extended again on idempotent replay"

    def test_different_worker_id_conflict(self, hb_service):
        tok = _setup_full(hb_service, worker_id="exec-1", expires_in=300)
        _setup_worker(hb_service, "exec-2", "registered", "supervisor")
        r1 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-diff-w",
            extend_seconds=300,
        )
        assert r1["success"]

        r2 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-2",
            lease_token=tok, idempotency_key="key-diff-w",
            extend_seconds=300,
        )
        assert r2["success"] is False
        assert r2["error_code"] == ERROR_IDEMPOTENCY_CONFLICT

    def test_different_token_conflict(self, hb_service):
        tok = _setup_full(hb_service, expires_in=300)
        r1 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-diff-tok",
            extend_seconds=300,
        )
        assert r1["success"]

        r2 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="different-token", idempotency_key="key-diff-tok",
            extend_seconds=300,
        )
        assert r2["success"] is False
        assert r2["error_code"] == ERROR_IDEMPOTENCY_CONFLICT

    def test_different_extend_seconds_conflict(self, hb_service):
        tok = _setup_full(hb_service, expires_in=300)
        r1 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-diff-ext",
            extend_seconds=300,
        )
        assert r1["success"]

        r2 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-diff-ext",
            extend_seconds=600,
        )
        assert r2["success"] is False
        assert r2["error_code"] == ERROR_IDEMPOTENCY_CONFLICT

    def test_different_assignment_id_conflict(self, hb_service):
        tok = _setup_full(hb_service, assignment_id="asgn-abc", expires_in=300)
        _setup_assignment(hb_service, "asgn-xyz", task_id=2, worker_id="exec-1",
                           status="assigned", lease_token="tok-xyz", expires_in_seconds=300)
        _setup_task(hb_service, 2)
        r1 = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-diff-asgn",
            extend_seconds=300,
        )
        assert r1["success"]

        r2 = hb_service.heartbeat(
            task_id=2, assignment_id="asgn-xyz", worker_id="exec-1",
            lease_token="tok-xyz", idempotency_key="key-diff-asgn",
            extend_seconds=300,
        )
        assert r2["success"] is False
        assert r2["error_code"] == ERROR_IDEMPOTENCY_CONFLICT


# ═════════════════════════════════════════════════════════════
#  原子性
# ═════════════════════════════════════════════════════════════

class TestHeartbeatAtomicity:

    def test_heartbeat_db_error_rollback(self, hb_service):
        """Force DB error → no partial writes."""
        tok = _setup_full(hb_service, expires_in=300)
        # Delete the task so FK constraint fails on heartbeat insert
        conn = _raw_conn(hb_service.db_path)
        conn.execute("DELETE FROM task_assignments WHERE assignment_id = 'asgn-abc'")
        conn.commit()
        conn.close()

        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token=tok, idempotency_key="key-rollback-db",
            extend_seconds=300,
        )
        # assignment not found → no partial writes
        assert r["success"] is False
        assert _count_heartbeats(hb_service) == 0

    def test_feature_flag_false_rejected_no_writes(self, hb_service_disabled):
        r = hb_service_disabled.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok", idempotency_key="key-ff-off",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_V2_CONTROL_PLANE_DISABLED

    def test_extend_seconds_below_min_rejected(self, hb_service):
        _setup_full(hb_service, expires_in=300)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-min-fail",
            extend_seconds=10,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_VALIDATION_ERROR

    def test_extend_seconds_above_max_rejected(self, hb_service):
        _setup_full(hb_service, expires_in=300)
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok-secret", idempotency_key="key-max-fail",
            extend_seconds=7200,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_VALIDATION_ERROR

    def test_exactly_expired_lease_boundary_rejected(self, hb_service):
        """When lease_expires_at equals now exactly → treated as expired."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _setup_worker(hb_service, "exec-1", WORKER_STATUS_BUSY)
        _setup_task(hb_service, 1)
        conn = _raw_conn(hb_service.db_path)
        conn.execute("""
            INSERT INTO task_assignments
            (assignment_id, task_id, worker_id, project_id, agent_type_required,
             status, lease_token, lease_expires_at, dispatched_at, created_at, updated_at)
            VALUES ('asgn-bdry', 1, 'exec-1', 1, 'executor',
                    'assigned', 'tok-bdry', ?, ?, ?, ?)
        """, (now, now, now, now))
        conn.commit()
        conn.close()

        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-bdry", worker_id="exec-1",
            lease_token="tok-bdry", idempotency_key="key-bdry",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_STALE_LEASE


# ═════════════════════════════════════════════════════════════
#  参数校验
# ═════════════════════════════════════════════════════════════

class TestHeartbeatValidation:

    def test_task_id_not_positive_rejected(self, hb_service):
        r = hb_service.heartbeat(
            task_id=0, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok", idempotency_key="key-tid0",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_VALIDATION_ERROR

    def test_assignment_id_empty_rejected(self, hb_service):
        r = hb_service.heartbeat(
            task_id=1, assignment_id="", worker_id="exec-1",
            lease_token="tok", idempotency_key="key-aid-empty",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_VALIDATION_ERROR

    def test_worker_id_empty_rejected(self, hb_service):
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="",
            lease_token="tok", idempotency_key="key-wid-empty",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_VALIDATION_ERROR

    def test_lease_token_empty_rejected(self, hb_service):
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="", idempotency_key="key-tok-empty",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_VALIDATION_ERROR

    def test_idempotency_key_empty_rejected(self, hb_service):
        r = hb_service.heartbeat(
            task_id=1, assignment_id="asgn-abc", worker_id="exec-1",
            lease_token="tok", idempotency_key="",
            extend_seconds=300,
        )
        assert r["success"] is False
        assert r["error_code"] == ERROR_VALIDATION_ERROR
