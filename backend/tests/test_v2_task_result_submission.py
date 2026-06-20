"""V2.0-B3a TaskResultSubmissionService tests.

All tests use temporary SQLite databases. They do not touch the production DB,
do not run the Executor, and do not call external AI services.
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

from app.supervisor.task_result_submission_service import (
    TaskResultSubmissionService,
    ERROR_ARTIFACT_INVALID,
    ERROR_IDEMPOTENCY_CONFLICT,
    ERROR_LEASE_CONFLICT,
    ERROR_RESULT_PACKET_INVALID,
    ERROR_STALE_LEASE,
    ERROR_STATE_VERSION_CONFLICT,
    ERROR_TASK_NOT_SUBMITTABLE,
    ERROR_V2_CONTROL_PLANE_DISABLED,
    ERROR_WORKER_NOT_REGISTERED,
    ERROR_WORKER_TYPE_NOT_ALLOWED,
)


SCHEMA = """
CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'draft'
);

CREATE TABLE development_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    title TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',
    state_version INTEGER DEFAULT 1,
    last_state_change TEXT,
    files_to_modify TEXT DEFAULT '[]',
    files_to_check TEXT DEFAULT '[]',
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE agent_workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL UNIQUE,
    worker_type TEXT NOT NULL CHECK (worker_type IN ('executor','supervisor','reviewer')),
    status TEXT NOT NULL DEFAULT 'registered'
        CHECK (status IN ('registered','available','busy','offline','disabled')),
    current_load INTEGER DEFAULT 0,
    version INTEGER DEFAULT 1,
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE task_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL UNIQUE,
    task_id INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('assigned','acknowledged','running','completed','failed','timeout','retrying','cancelled')),
    lease_token TEXT,
    lease_expires_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES development_tasks(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE task_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id TEXT NOT NULL UNIQUE,
    task_id INTEGER NOT NULL,
    assignment_id TEXT NOT NULL UNIQUE,
    worker_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    result_status TEXT NOT NULL CHECK (result_status IN ('submitted','verified','rework','blocked','failed','timeout')),
    files_modified_json TEXT DEFAULT '[]',
    files_checked_json TEXT DEFAULT '[]',
    diff_summary TEXT DEFAULT '',
    tests_total INTEGER DEFAULT 0,
    tests_passed INTEGER DEFAULT 0,
    tests_failed INTEGER DEFAULT 0,
    tests_skipped INTEGER DEFAULT 0,
    test_output TEXT DEFAULT '',
    git_commit TEXT DEFAULT '',
    git_branch TEXT DEFAULT '',
    base_commit TEXT DEFAULT '',
    exit_code INTEGER,
    error_message TEXT,
    stdout TEXT DEFAULT '',
    stderr TEXT DEFAULT '',
    model_calls INTEGER DEFAULT 0,
    repair_attempts INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    workspace_path TEXT DEFAULT '',
    manual_actions_json TEXT DEFAULT '[]',
    evidence_refs_json TEXT DEFAULT '[]',
    handoff_requested INTEGER DEFAULT 0,
    remaining_steps_json TEXT DEFAULT '[]',
    idempotency_key TEXT UNIQUE,
    submitted_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE execution_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    result_id TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    assignment_id TEXT NOT NULL DEFAULT '',
    project_id INTEGER NOT NULL,
    artifact_type TEXT NOT NULL CHECK (artifact_type IN ('diff','log','test_report','git_commit','screenshot','build_output','lint_report','coverage_report','binary','document','other')),
    artifact_subtype TEXT,
    storage_path TEXT NOT NULL,
    storage_url TEXT,
    content_hash TEXT,
    size_bytes INTEGER,
    mime_type TEXT,
    description TEXT DEFAULT '',
    tags_json TEXT DEFAULT '[]',
    is_sensitive INTEGER DEFAULT 0,
    retention_policy TEXT DEFAULT 'permanent',
    metadata_json TEXT DEFAULT '{}',
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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
"""


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_submit_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO projects (id, name) VALUES (1, 'p')")
    conn.commit()
    conn.close()
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


def conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def setup_running(db_path, *, worker_type="executor", assignment_status="running", task_status="running",
                  version=3, expires_seconds=300, worker_id="exec-1", task_id=10):
    c = conn(db_path)
    expires = (datetime.now() + timedelta(seconds=expires_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status, current_load) VALUES (?, ?, 'busy', 1)",
              (worker_id, worker_type))
    c.execute("""
        INSERT INTO development_tasks
        (id, project_id, title, status, state_version, files_to_modify, files_to_check)
        VALUES (?, 1, 'task', ?, ?, '["src/a.py"]', '["tests/test_a.py"]')
    """, (task_id, task_status, version))
    c.execute("""
        INSERT INTO task_assignments
        (assignment_id, task_id, worker_id, project_id, status, lease_token, lease_expires_at)
        VALUES ('asgn-1', ?, ?, 1, ?, 'lease-secret', ?)
    """, (task_id, worker_id, assignment_status, expires))
    c.commit()
    c.close()


def packet(**overrides):
    base = {
        "execution_id": "asgn-1",
        "assignment_id": "asgn-1",
        "task_id": 10,
        "worker_id": "exec-1",
        "result_status": "submitted",
        "files_modified": ["src/a.py"],
        "files_checked": ["tests/test_a.py"],
        "diff_summary": "+1 -0",
        "tests": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "output": "1 passed"},
        "git_commit": "abcdef1234567890",
        "git_branch": "task/10",
        "base_commit": "1234567",
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "manual_actions": [],
        "errors": [],
        "evidence_refs": ["artifact-diff-1"],
        "artifacts": [{
            "artifact_id": "artifact-diff-1",
            "artifact_type": "diff",
            "uri": "artifacts/task-10/diff.patch",
            "sha256": "a" * 64,
            "size_bytes": 12,
            "mime_type": "text/x-diff",
            "metadata": {"kind": "diff"},
        }],
        "handoff_requested": False,
        "remaining_steps": [],
        "submitted_at": "2026-06-20T00:00:00Z",
        "duration_ms": 10,
        "model_calls": 0,
        "repair_attempts": 0,
    }
    base.update(overrides)
    return base


def submit(db_path, **overrides):
    svc = TaskResultSubmissionService(db_path, v2_enabled=True)
    return svc.submit_result(
        task_id=overrides.pop("task_id", 10),
        assignment_id=overrides.pop("assignment_id", "asgn-1"),
        worker_id=overrides.pop("worker_id", "exec-1"),
        lease_token=overrides.pop("lease_token", "lease-secret"),
        expected_version=overrides.pop("expected_version", 3),
        idempotency_key=overrides.pop("idempotency_key", "idem-1"),
        result_packet=overrides.pop("result_packet", packet()),
    )


def count(db_path, table):
    c = conn(db_path)
    try:
        return c.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        c.close()


def test_running_task_submit_success_persists_result_artifacts_state_event_assignment_worker(db_path):
    setup_running(db_path)
    result = submit(db_path)

    assert result["success"] is True
    assert result["task_state"] == "RESULT_SUBMITTED"
    assert result["state_version"] == 4
    assert result["assignment_status"] == "completed"
    assert result["worker_status"] == "available"
    assert result["artifact_count"] == 1
    assert result["result_summary"]["tests"]["passed"] == 1

    c = conn(db_path)
    assert c.execute("SELECT result_status FROM task_results").fetchone()["result_status"] == "submitted"
    assert c.execute("SELECT artifact_id FROM execution_artifacts").fetchone()["artifact_id"] == "artifact-diff-1"
    assert c.execute("SELECT status, state_version FROM development_tasks WHERE id=10").fetchone()["status"] == "result_submitted"
    assert c.execute("SELECT status FROM task_assignments WHERE assignment_id='asgn-1'").fetchone()["status"] == "completed"
    assert c.execute("SELECT status FROM agent_workers WHERE worker_id='exec-1'").fetchone()["status"] == "available"
    assert c.execute("SELECT event_type FROM task_events").fetchone()["event_type"] == "submit"
    c.close()


def test_feature_flag_false_writes_nothing(db_path):
    setup_running(db_path)
    svc = TaskResultSubmissionService(db_path, v2_enabled=False)
    result = svc.submit_result(10, "asgn-1", "exec-1", "lease-secret", 3, "idem-off", packet())

    assert result["error_code"] == ERROR_V2_CONTROL_PLANE_DISABLED
    assert count(db_path, "task_results") == 0
    assert count(db_path, "execution_artifacts") == 0
    assert count(db_path, "task_events") == 0


@pytest.mark.parametrize("mutator,code", [
    (lambda db: None, ERROR_WORKER_NOT_REGISTERED),
])
def test_unregistered_worker_rejected(db_path, mutator, code):
    setup_running(db_path)
    result = submit(db_path, worker_id="ghost", result_packet=packet(worker_id="ghost"))
    assert result["error_code"] == code


def test_non_executor_worker_rejected(db_path):
    setup_running(db_path, worker_type="reviewer")
    assert submit(db_path)["error_code"] == ERROR_WORKER_TYPE_NOT_ALLOWED


@pytest.mark.parametrize("kwargs,expected", [
    ({"worker_id": "exec-2", "result_packet": packet(worker_id="exec-2")}, ERROR_LEASE_CONFLICT),
    ({"lease_token": "wrong"}, ERROR_LEASE_CONFLICT),
])
def test_wrong_worker_or_token_rejected(db_path, kwargs, expected):
    setup_running(db_path)
    if kwargs.get("worker_id") == "exec-2":
        c = conn(db_path)
        c.execute("INSERT INTO agent_workers (worker_id, worker_type, status) VALUES ('exec-2','executor','busy')")
        c.commit()
        c.close()
    assert submit(db_path, **kwargs)["error_code"] == expected


def test_assignment_not_found_rejected(db_path):
    setup_running(db_path)
    assert submit(db_path, assignment_id="missing", result_packet=packet(assignment_id="missing"))["error_code"] == "ASSIGNMENT_NOT_FOUND"


def test_expired_lease_rejected(db_path):
    setup_running(db_path, expires_seconds=-5)
    assert submit(db_path)["error_code"] == ERROR_STALE_LEASE


@pytest.mark.parametrize("task_status,assignment_status,code", [
    ("queued", "running", ERROR_TASK_NOT_SUBMITTABLE),
    ("running", "completed", ERROR_TASK_NOT_SUBMITTABLE),
])
def test_non_running_task_or_assignment_rejected(db_path, task_status, assignment_status, code):
    setup_running(db_path, task_status=task_status, assignment_status=assignment_status)
    assert submit(db_path)["error_code"] == code


def test_expected_version_conflict_rejected(db_path):
    setup_running(db_path, version=4)
    assert submit(db_path)["error_code"] == ERROR_STATE_VERSION_CONFLICT


@pytest.mark.parametrize("bad_packet", [
    packet(result_status="verified"),
    packet(files_modified=["../secret.py"]),
    packet(files_modified=["src/outside.py"]),
    packet(git_commit="not-a-sha"),
    packet(evidence_refs=["missing-artifact"]),
    packet(artifacts=[{"artifact_id": "artifact-diff-1", "artifact_type": "diff", "uri": "../x", "sha256": "a" * 64}]),
    packet(manual_actions=[{"token": "plain"}]),
])
def test_invalid_result_packets_rejected(db_path, bad_packet):
    setup_running(db_path)
    result = submit(db_path, result_packet=bad_packet)
    assert result["error_code"] in {ERROR_RESULT_PACKET_INVALID, ERROR_ARTIFACT_INVALID}
    assert count(db_path, "task_results") == 0


@pytest.mark.parametrize("uri", [
    r"C:\Sandbox\Desktop\secret.db",
    "/var/tmp/secret.db",
    r"\\server\share\secret.db",
    "file://artifacts/task-10/diff.patch",
    "http://example.com/diff.patch",
    "https://example.com/diff.patch",
    "../artifacts/diff.patch",
    "artifacts/%2e%2e/secret.patch",
    "",
    "a" * 513,
])
def test_artifact_attack_paths_are_rejected(db_path, uri):
    setup_running(db_path)
    bad = packet(artifacts=[{
        "artifact_id": "artifact-diff-1",
        "artifact_type": "diff",
        "uri": uri,
        "sha256": "a" * 64,
        "size_bytes": 1,
    }])

    result = submit(db_path, result_packet=bad)

    assert result["error_code"] in {ERROR_ARTIFACT_INVALID, ERROR_RESULT_PACKET_INVALID}
    assert count(db_path, "execution_artifacts") == 0


@pytest.mark.parametrize("artifact_update", [
    {"sha256": "not-sha"},
    {"size_bytes": -1},
    {"metadata": {"api_key": "secret"}},
    {"metadata": {"lease_token": "secret"}},
    {"metadata": {"DATABASE_URL": "sqlite:///secret.db"}},
    {"metadata": {"password": "secret"}},
    {"metadata": {"Authorization Header": "Bearer secret"}},
    {"content": b"binary-data".hex()},
    {"content_base64": "AAAA"},
    {"bytes": [1, 2, 3]},
])
def test_artifact_metadata_attacks_are_rejected(db_path, artifact_update):
    setup_running(db_path)
    artifact = {
        "artifact_id": "artifact-diff-1",
        "artifact_type": "diff",
        "uri": "artifacts/task-10/diff.patch",
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    artifact.update(artifact_update)
    bad = packet(artifacts=[artifact])

    result = submit(db_path, result_packet=bad)

    assert result["error_code"] in {ERROR_ARTIFACT_INVALID, ERROR_RESULT_PACKET_INVALID}
    assert count(db_path, "execution_artifacts") == 0


def test_missing_required_packet_field_rejected(db_path):
    setup_running(db_path)
    bad = packet()
    bad.pop("tests")
    assert submit(db_path, result_packet=bad)["error_code"] == ERROR_RESULT_PACKET_INVALID


def test_failed_test_result_can_be_submitted_without_git_commit(db_path):
    setup_running(db_path)
    failed_packet = packet(
        tests={"total": 2, "passed": 1, "failed": 1, "skipped": 0, "output": "failed"},
        git_commit="",
        exit_code=1,
        errors=[{"step": "test_execution", "message": "failed"}],
    )
    assert submit(db_path, result_packet=failed_packet)["success"] is True


def test_plain_lease_token_is_not_persisted_in_result_or_artifact(db_path):
    setup_running(db_path)
    submit(db_path)
    c = conn(db_path)
    blob = "\n".join(
        str(row[0]) for row in c.execute(
            "SELECT files_modified_json || evidence_refs_json || COALESCE(error_message,'') FROM task_results"
        )
    )
    blob += "\n".join(str(row[0]) for row in c.execute("SELECT metadata_json FROM execution_artifacts"))
    c.close()
    assert "lease-secret" not in blob


def test_idempotent_repeat_returns_first_result_without_duplicate_writes(db_path):
    setup_running(db_path)
    first = submit(db_path)
    second = submit(db_path)

    assert second["success"] is True
    assert second["idempotent"] is True
    assert second["result_id"] == first["result_id"]
    assert count(db_path, "task_results") == 1
    assert count(db_path, "execution_artifacts") == 1
    assert count(db_path, "task_events") == 1
    c = conn(db_path)
    assert c.execute("SELECT state_version FROM development_tasks WHERE id=10").fetchone()["state_version"] == 4
    c.close()


def test_worker_remains_busy_when_other_active_assignment_exists(db_path):
    setup_running(db_path)
    c = conn(db_path)
    expires = (datetime.now() + timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
        INSERT INTO development_tasks
        (id, project_id, title, status, state_version, files_to_modify, files_to_check)
        VALUES (11, 1, 'other', 'running', 2, '["src/b.py"]', '[]')
    """)
    c.execute("""
        INSERT INTO task_assignments
        (assignment_id, task_id, worker_id, project_id, status, lease_token, lease_expires_at)
        VALUES ('asgn-2', 11, 'exec-1', 1, 'running', 'lease-other', ?)
    """, (expires,))
    c.commit()
    c.close()

    first = submit(db_path)
    second = submit(db_path)

    assert first["success"] is True
    assert first["worker_status"] == "busy"
    assert second["idempotent"] is True
    assert second["worker_status"] == "busy"
    c = conn(db_path)
    assert c.execute("SELECT status FROM agent_workers WHERE worker_id='exec-1'").fetchone()["status"] == "busy"
    c.close()


@pytest.mark.parametrize("kwargs", [
    {"result_packet": packet(diff_summary="changed")},
    {"result_packet": packet(evidence_refs=["artifact-diff-1", "artifact-log-1"], artifacts=[
        {"artifact_id": "artifact-diff-1", "artifact_type": "diff", "uri": "artifacts/task-10/diff.patch", "sha256": "a" * 64},
        {"artifact_id": "artifact-log-1", "artifact_type": "log", "uri": "artifacts/task-10/run.log", "sha256": "b" * 64},
    ])},
    {"expected_version": 4},
])
def test_same_idempotency_key_with_changed_request_conflicts(db_path, kwargs):
    setup_running(db_path)
    assert submit(db_path)["success"] is True
    assert submit(db_path, **kwargs)["error_code"] == ERROR_IDEMPOTENCY_CONFLICT


@pytest.mark.parametrize("preinsert_sql", [
    "INSERT INTO task_results (result_id, task_id, assignment_id, worker_id, project_id, result_status, idempotency_key) VALUES ('existing',10,'asgn-1','exec-1',1,'submitted','other')",
    "INSERT INTO execution_artifacts (artifact_id, result_id, task_id, assignment_id, project_id, artifact_type, storage_path) VALUES ('artifact-diff-1','other',10,'asgn-x',1,'diff','x')",
])
def test_insert_failures_roll_back_all_later_state(db_path, preinsert_sql):
    setup_running(db_path)
    c = conn(db_path)
    c.execute(preinsert_sql)
    c.commit()
    c.close()

    before_results = count(db_path, "task_results")
    before_artifacts = count(db_path, "execution_artifacts")
    result = submit(db_path)

    assert result["error_code"] == "INTERNAL_ERROR"
    assert count(db_path, "task_results") == before_results
    assert count(db_path, "execution_artifacts") == before_artifacts
    c = conn(db_path)
    task = c.execute("SELECT status, state_version FROM development_tasks WHERE id=10").fetchone()
    assignment = c.execute("SELECT status FROM task_assignments WHERE assignment_id='asgn-1'").fetchone()
    worker = c.execute("SELECT status FROM agent_workers WHERE worker_id='exec-1'").fetchone()
    c.close()
    assert task["status"] == "running"
    assert task["state_version"] == 3
    assert assignment["status"] == "running"
    assert worker["status"] == "busy"


def test_two_concurrent_submits_only_one_writes(db_path):
    setup_running(db_path)
    results = []

    def run(key):
        results.append(submit(db_path, idempotency_key=key))

    t1 = threading.Thread(target=run, args=("idem-a",))
    t2 = threading.Thread(target=run, args=("idem-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sum(1 for r in results if r.get("success")) == 1
    assert count(db_path, "task_results") == 1
    assert count(db_path, "execution_artifacts") == 1
    assert count(db_path, "task_events") == 1
