"""V2.0-B4 TaskHandoffService tests.

All tests use temporary SQLite databases. They do not touch production data,
start Executors, execute Task 32-35, or call external AI services.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.supervisor.task_handoff_service import (
    TaskHandoffService,
    ERROR_HANDOFF_CONFLICT,
    ERROR_HANDOFF_EXPIRED,
    ERROR_HANDOFF_NOT_ALLOWED,
    ERROR_IDEMPOTENCY_CONFLICT,
    ERROR_LEASE_CONFLICT,
    ERROR_STALE_LEASE,
    ERROR_STATE_VERSION_CONFLICT,
    ERROR_VALIDATION_ERROR,
    ERROR_V2_CONTROL_PLANE_DISABLED,
    ERROR_WORKER_CAPABILITY_MISMATCH,
    ERROR_WORKER_NOT_AVAILABLE,
    ERROR_WORKER_TYPE_NOT_ALLOWED,
)


SCHEMA = """
CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL);

CREATE TABLE development_tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    title TEXT DEFAULT '',
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 1,
    implementation_steps TEXT DEFAULT '{}',
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE agent_workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL UNIQUE,
    worker_type TEXT NOT NULL CHECK (worker_type IN ('executor','supervisor','reviewer')),
    status TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('registered','available','busy','offline','disabled')),
    current_load INTEGER DEFAULT 0,
    max_concurrency INTEGER DEFAULT 1,
    version INTEGER DEFAULT 1,
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE agent_capabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    capability TEXT NOT NULL
);

CREATE TABLE task_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL UNIQUE,
    task_id INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    agent_type_required TEXT NOT NULL DEFAULT 'executor',
    decision_reason TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('assigned','acknowledged','running','completed','failed','timeout','retrying','cancelled')),
    lease_token TEXT,
    lease_expires_at TEXT,
    idempotency_key TEXT,
    dispatched_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id INTEGER NOT NULL,
    assignment_id TEXT,
    project_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    reason TEXT DEFAULT '',
    detail_json TEXT DEFAULT '{}',
    operator_type TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    state_version_before INTEGER,
    state_version_after INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE task_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    handoff_id TEXT NOT NULL UNIQUE,
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
    idempotency_key TEXT UNIQUE,
    request_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
"""


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_handoff_")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA)
    c.execute("INSERT INTO projects VALUES (1, 'p')")
    c.commit()
    c.close()
    try:
        yield path
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass
            except PermissionError:
                pass


def conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def service(path, enabled=True):
    return TaskHandoffService(path, v2_enabled=enabled)


def setup_running(path, *, task_status="running", assignment_status="running", lease_expired=False, worker_status="busy"):
    c = conn(path)
    expires = datetime.now() - timedelta(seconds=5) if lease_expired else datetime.now() + timedelta(seconds=300)
    c.execute("INSERT INTO development_tasks VALUES (10, 1, 'task', ?, 3, '{\"_requirements\":{\"lang\":\"python\"}}')", (task_status,))
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status, current_load) VALUES ('exec-1','executor',?,1)", (worker_status,))
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status, current_load) VALUES ('exec-2','executor','available',0)")
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status, current_load) VALUES ('rev-1','reviewer','available',0)")
    c.execute("INSERT INTO agent_capabilities (worker_id, capability) VALUES ('exec-2','python')")
    c.execute("""
        INSERT INTO task_assignments
        (assignment_id, task_id, worker_id, project_id, status, lease_token, lease_expires_at)
        VALUES ('asgn-1', 10, 'exec-1', 1, ?, 'lease-secret', ?)
    """, (assignment_status, expires.strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    c.close()


def request(path, **kw):
    return service(path).request_handoff(
        task_id=kw.get("task_id", 10),
        assignment_id=kw.get("assignment_id", "asgn-1"),
        from_worker_id=kw.get("from_worker_id", "exec-1"),
        lease_token=kw.get("lease_token", "lease-secret"),
        reason_code=kw.get("reason_code", "CAPABILITY_MISMATCH"),
        reason=kw.get("reason", "needs another executor"),
        completed_steps=kw.get("completed_steps", ["read code"]),
        remaining_steps=kw.get("remaining_steps", ["finish tests"]),
        recent_errors=kw.get("recent_errors", []),
        evidence_refs=kw.get("evidence_refs", []),
        forbidden_actions=kw.get("forbidden_actions", ["do not run executor"]),
        idempotency_key=kw.get("idempotency_key", "handoff-request-1"),
        files_changed=kw.get("files_changed", ["backend/app/a.py"]),
        tests_run=kw.get("tests_run", [{"cmd": "pytest", "status": "failed"}]),
        context_snapshot=kw.get("context_snapshot", {"note": "safe"}),
        git_head=kw.get("git_head", "a" * 40),
    )


def count(path, table):
    c = conn(path)
    try:
        return c.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        c.close()


def test_request_handoff_saves_packet_and_event(db_path):
    setup_running(db_path)
    result = request(db_path)
    assert result["success"] is True
    assert result["status"] == "pending"
    c = conn(db_path)
    row = c.execute("SELECT * FROM task_handoffs").fetchone()
    assert row["reason_code"] == "CAPABILITY_MISMATCH"
    assert "lease-secret" not in "".join(str(row[k]) for k in row.keys())
    assert c.execute("SELECT event_type FROM task_events").fetchone()["event_type"] == "handoff"
    c.close()


@pytest.mark.parametrize("field,value,code", [
    ("from_worker_id", "other-worker", ERROR_LEASE_CONFLICT),
    ("lease_token", "wrong-token", ERROR_LEASE_CONFLICT),
    ("assignment_id", "missing", "ASSIGNMENT_NOT_FOUND"),
    ("reason_code", "SUCCESS", ERROR_HANDOFF_NOT_ALLOWED),
])
def test_request_rejects_invalid_source_or_reason(db_path, field, value, code):
    setup_running(db_path)
    result = request(db_path, **{field: value})
    assert result["success"] is False
    assert result["error_code"] == code


def test_request_rejects_expired_lease(db_path):
    setup_running(db_path, lease_expired=True)
    assert request(db_path)["error_code"] == ERROR_STALE_LEASE


def test_request_rejects_disallowed_task_state(db_path):
    setup_running(db_path, task_status="verified")
    assert request(db_path)["error_code"] == ERROR_HANDOFF_NOT_ALLOWED


@pytest.mark.parametrize("files_changed,context_snapshot", [
    (["C:/SandboxUser/local/secret.py"], {}),
    (["/etc/passwd"], {}),
    (["..%2fsecret.py"], {}),
    (["file://secret"], {}),
    (["a" * 513], {}),
    (["safe.py"], {"DATABASE_URL": "sqlite:///secret.db"}),
    (["safe.py"], {"Authorization": "Bearer token"}),
])
def test_request_rejects_unsafe_packet(db_path, files_changed, context_snapshot):
    setup_running(db_path)
    result = request(db_path, files_changed=files_changed, context_snapshot=context_snapshot, idempotency_key=f"bad-{len(str(files_changed))}-{len(str(context_snapshot))}")
    assert result["success"] is False
    assert result["error_code"] == ERROR_VALIDATION_ERROR


def test_request_idempotent_and_conflict(db_path):
    setup_running(db_path)
    first = request(db_path)
    repeat = request(db_path)
    conflict = request(db_path, reason="different")
    assert first["success"] is True
    assert repeat["idempotent"] is True
    assert conflict["error_code"] == ERROR_IDEMPOTENCY_CONFLICT
    assert count(db_path, "task_handoffs") == 1


def test_accept_handoff_creates_new_assignment_and_releases_old_worker(db_path):
    setup_running(db_path)
    handoff = request(db_path)
    result = service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 3, "accept-1")
    assert result["success"] is True
    assert result["status"] == "accepted"
    assert result["lease_token"]
    c = conn(db_path)
    assert c.execute("SELECT status FROM task_handoffs").fetchone()["status"] == "accepted"
    assert c.execute("SELECT status FROM task_assignments WHERE assignment_id='asgn-1'").fetchone()["status"] == "cancelled"
    new_row = c.execute("SELECT status, lease_token FROM task_assignments WHERE assignment_id=?", (result["assignment_id"],)).fetchone()
    assert new_row["status"] == "running"
    assert new_row["lease_token"] != "lease-secret"
    assert c.execute("SELECT status FROM agent_workers WHERE worker_id='exec-2'").fetchone()["status"] == "busy"
    assert c.execute("SELECT status FROM agent_workers WHERE worker_id='exec-1'").fetchone()["status"] == "registered"
    assert c.execute("SELECT status FROM development_tasks WHERE id=10").fetchone()["status"] == "running"
    c.close()


def test_accept_keeps_old_worker_busy_when_other_active_assignment_exists(db_path):
    setup_running(db_path)
    c = conn(db_path)
    c.execute("INSERT INTO development_tasks VALUES (11, 1, 'other', 'running', 1, '{}')")
    c.execute("""
        INSERT INTO task_assignments
        (assignment_id, task_id, worker_id, project_id, status, lease_token, lease_expires_at)
        VALUES ('asgn-other', 11, 'exec-1', 1, 'running', 'other', ?)
    """, ((datetime.now() + timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S"),))
    c.commit()
    c.close()
    handoff = request(db_path)
    assert service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 3, "accept-busy")["success"] is True
    c = conn(db_path)
    assert c.execute("SELECT status FROM agent_workers WHERE worker_id='exec-1'").fetchone()["status"] == "busy"
    c.close()


@pytest.mark.parametrize("worker_update,code", [
    ("UPDATE agent_workers SET status='busy' WHERE worker_id='exec-2'", ERROR_WORKER_NOT_AVAILABLE),
    ("UPDATE agent_workers SET status='offline' WHERE worker_id='exec-2'", ERROR_WORKER_NOT_AVAILABLE),
    ("UPDATE agent_workers SET worker_type='reviewer' WHERE worker_id='exec-2'", ERROR_WORKER_TYPE_NOT_ALLOWED),
])
def test_accept_rejects_unavailable_or_wrong_type_worker(db_path, worker_update, code):
    setup_running(db_path)
    handoff = request(db_path)
    c = conn(db_path)
    c.execute(worker_update)
    c.commit()
    c.close()
    assert service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 3, "accept-invalid")["error_code"] == code


def test_accept_rejects_capability_mismatch(db_path):
    setup_running(db_path)
    handoff = request(db_path)
    c = conn(db_path)
    c.execute("DELETE FROM agent_capabilities WHERE worker_id='exec-2'")
    c.commit()
    c.close()
    assert service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 3, "accept-cap")["error_code"] == ERROR_WORKER_CAPABILITY_MISMATCH


def test_accept_expected_version_conflict_and_expired_handoff(db_path):
    setup_running(db_path)
    handoff = request(db_path)
    assert service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 2, "accept-version")["error_code"] == ERROR_STATE_VERSION_CONFLICT
    c = conn(db_path)
    c.execute("UPDATE task_handoffs SET expires_at=? WHERE handoff_id=?", ((datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), handoff["handoff_id"]))
    c.commit()
    c.close()
    assert service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 3, "accept-expired")["error_code"] == ERROR_HANDOFF_EXPIRED


def test_accept_idempotent_and_conflict(db_path):
    setup_running(db_path)
    handoff = request(db_path)
    first = service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 3, "accept-idem")
    repeat = service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 3, "accept-idem")
    conflict = service(db_path).accept_handoff(handoff["handoff_id"], "exec-2", 4, "accept-idem")
    assert first["success"] is True
    assert repeat["idempotent"] is True
    assert conflict["error_code"] == ERROR_IDEMPOTENCY_CONFLICT
    assert count(db_path, "task_assignments") == 2


def test_concurrent_accept_only_one_succeeds(db_path):
    setup_running(db_path)
    handoff = request(db_path)
    results = []

    def run(worker, key):
        results.append(service(db_path).accept_handoff(handoff["handoff_id"], worker, 3, key))

    c = conn(db_path)
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status, current_load) VALUES ('exec-3','executor','available',0)")
    c.execute("INSERT INTO agent_capabilities (worker_id, capability) VALUES ('exec-3','python')")
    c.commit()
    c.close()
    threads = [threading.Thread(target=run, args=("exec-2", "concurrent-a")), threading.Thread(target=run, args=("exec-3", "concurrent-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r.get("success")) == 1
    assert count(db_path, "task_assignments") == 2


def test_reject_cancel_and_expire(db_path):
    setup_running(db_path)
    handoff = request(db_path)
    hid = handoff["handoff_id"]
    c = conn(db_path)
    c.execute("UPDATE task_handoffs SET to_worker_id='exec-2' WHERE handoff_id=?", (hid,))
    c.commit()
    c.close()
    assert service(db_path).reject_handoff(hid, "exec-1", "no", "reject-wrong")["error_code"] == ERROR_HANDOFF_NOT_ALLOWED
    assert service(db_path).reject_handoff(hid, "exec-2", "no", "reject-ok")["status"] == "rejected"

    handoff2 = request(db_path, idempotency_key="handoff-request-2")
    assert service(db_path).cancel_handoff(handoff2["handoff_id"], "exec-1", "cancel", "cancel-ok")["status"] == "cancelled"

    handoff3 = request(db_path, idempotency_key="handoff-request-3")
    c = conn(db_path)
    c.execute("UPDATE task_handoffs SET expires_at=? WHERE handoff_id=?", ((datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), handoff3["handoff_id"]))
    c.commit()
    c.close()
    assert service(db_path).expire_handoffs("expire-1")["expired_count"] == 1
    assert service(db_path).expire_handoffs("expire-1")["idempotent"] is True
    assert count(db_path, "task_handoffs") == 3


def test_feature_flag_false_zero_writes(db_path):
    setup_running(db_path)
    result = service(db_path, enabled=False).request_handoff(
        10, "asgn-1", "exec-1", "lease-secret", "CAPABILITY_MISMATCH", "reason",
        ["done"], ["todo"], [], [], ["do not run executor"], "flag-off",
    )
    assert result["error_code"] == ERROR_V2_CONTROL_PLANE_DISABLED
    assert count(db_path, "task_handoffs") == 0
