"""V2.0-B5a deterministic supervisor orchestration tests."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.supervisor.orchestration_service import (
    SupervisorOrchestrationService,
    ERROR_V2_CONTROL_PLANE_DISABLED,
)


SCHEMA = """
CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL);

CREATE TABLE development_tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    task_type TEXT DEFAULT 'code',
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 1,
    files_to_modify TEXT DEFAULT '[]',
    files_to_check TEXT DEFAULT '[]',
    test_steps TEXT DEFAULT '[]',
    acceptance_criteria TEXT DEFAULT '[]',
    implementation_steps TEXT DEFAULT '{}',
    dependencies TEXT DEFAULT '[]',
    last_state_change TEXT,
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

CREATE TABLE worker_project_scopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    project_id INTEGER NOT NULL
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

CREATE TABLE task_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id TEXT NOT NULL UNIQUE,
    task_id INTEGER NOT NULL,
    assignment_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    result_status TEXT NOT NULL,
    tests_total INTEGER DEFAULT 0,
    tests_passed INTEGER DEFAULT 0,
    tests_failed INTEGER DEFAULT 0,
    tests_skipped INTEGER DEFAULT 0,
    evidence_refs_json TEXT DEFAULT '[]',
    idempotency_key TEXT
);

CREATE TABLE execution_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    result_id TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    assignment_id TEXT NOT NULL,
    project_id INTEGER NOT NULL,
    artifact_type TEXT NOT NULL,
    storage_path TEXT NOT NULL
);

CREATE TABLE review_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL UNIQUE,
    result_id TEXT NOT NULL UNIQUE,
    task_id INTEGER NOT NULL,
    project_id INTEGER,
    reviewer_type TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('REVIEWING','PASS','VERIFIED','REWORK','BLOCKED','NEED_USER')),
    reason TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    issues_json TEXT DEFAULT '[]',
    evidence_json TEXT DEFAULT '{}',
    evidence_refs_json TEXT DEFAULT '[]',
    risk_level TEXT DEFAULT 'low',
    user_action_required INTEGER DEFAULT 0,
    metadata_json TEXT DEFAULT '{}',
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

CREATE TABLE supervisor_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL UNIQUE,
    project_id INTEGER NOT NULL,
    task_id INTEGER,
    observed_state TEXT DEFAULT '',
    state_version INTEGER,
    planned_action TEXT NOT NULL,
    selected_actor_id TEXT DEFAULT '',
    dry_run INTEGER NOT NULL DEFAULT 0,
    result TEXT DEFAULT '',
    result_json TEXT DEFAULT '{}',
    idempotency_key TEXT UNIQUE,
    request_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_supervisor_")
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
    return SupervisorOrchestrationService(path, v2_enabled=enabled)


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def old():
    return (datetime.now() - timedelta(seconds=1000)).strftime("%Y-%m-%d %H:%M:%S")


def add_worker(path, worker_id, worker_type="executor", status="available", capability="python", project_scope=1, last_seen=None):
    c = conn(path)
    c.execute(
        "INSERT INTO agent_workers (worker_id, worker_type, status, current_load, max_concurrency, last_seen_at) VALUES (?, ?, ?, ?, 1, ?)",
        (worker_id, worker_type, status, 1 if status == "busy" else 0, last_seen or now()),
    )
    if capability:
        c.execute("INSERT INTO agent_capabilities (worker_id, capability) VALUES (?, ?)", (worker_id, capability))
    if project_scope is not None:
        c.execute("INSERT INTO worker_project_scopes (worker_id, project_id) VALUES (?, ?)", (worker_id, project_scope))
    c.commit()
    c.close()


def add_task(path, task_id=10, status="queued", version=1, requirement="python"):
    req = '{"_requirements":{"lang":"%s"}}' % requirement if requirement else '{}'
    c = conn(path)
    c.execute(
        "INSERT INTO development_tasks (id, project_id, title, status, state_version, implementation_steps) VALUES (?, 1, 'task', ?, ?, ?)",
        (task_id, status, version, req),
    )
    c.commit()
    c.close()


def add_result(path, task_id=10, worker_id="exec-1", result_id="rslt-1"):
    c = conn(path)
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status, current_load, last_seen_at) VALUES (?, 'executor', 'available', 0, ?)", (worker_id, now()))
    c.execute("""
        INSERT INTO task_results
        (result_id, task_id, assignment_id, worker_id, project_id, result_status, tests_total, tests_passed, tests_failed, evidence_refs_json)
        VALUES (?, ?, 'asgn-1', ?, 1, 'submitted', 1, 1, 0, '["art-1"]')
    """, (result_id, task_id, worker_id))
    c.execute("""
        INSERT INTO execution_artifacts
        (artifact_id, result_id, task_id, assignment_id, project_id, artifact_type, storage_path)
        VALUES ('art-1', ?, ?, 'asgn-1', 1, 'test_report', 'artifacts/report.txt')
    """, (result_id, task_id))
    c.commit()
    c.close()


def count(path, table):
    c = conn(path)
    try:
        return c.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        c.close()


def plan(path, key="plan-1"):
    return service(path).plan_next_action(1, key)


def test_queued_selects_available_executor(db_path):
    add_task(db_path, status="queued")
    add_worker(db_path, "exec-1")
    result = plan(db_path)
    assert result["planned_action"] == "CLAIM_TASK"
    assert result["selected_actor_id"] == "exec-1"


def test_result_submitted_selects_reviewer_not_executor(db_path):
    add_task(db_path, status="result_submitted", version=4)
    add_result(db_path)
    add_worker(db_path, "rev-1", worker_type="reviewer", capability=None)
    result = plan(db_path)
    assert result["planned_action"] == "BEGIN_REVIEW"
    assert result["selected_actor_id"] == "rev-1"
    assert result["result_id"] == "rslt-1"


@pytest.mark.parametrize("state,action", [
    ("rework", "PLAN_REWORK_HANDOFF"),
    ("need_user", "STOP_AND_WAIT_USER"),
    ("blocked", "STOP_AND_REPORT_BLOCKER"),
    ("claimed", "WAIT_WORKER"),
    ("running", "WAIT_WORKER"),
    ("reviewing", "WAIT_REVIEW_DECISION"),
    ("verified", "NO_ACTION"),
])
def test_state_priority_actions(db_path, state, action):
    add_task(db_path, status=state, version=2)
    add_worker(db_path, "exec-1")
    assert plan(db_path)["planned_action"] == action


@pytest.mark.parametrize("status", ["offline", "disabled", "busy"])
def test_unavailable_worker_not_selected(db_path, status):
    add_task(db_path, status="queued")
    add_worker(db_path, "exec-1", status=status)
    assert plan(db_path)["planned_action"] == "WAIT_EXECUTOR"


def test_capability_stale_heartbeat_and_project_scope_filter_workers(db_path):
    add_task(db_path, status="queued", requirement="python")
    add_worker(db_path, "wrong-cap", capability="node")
    add_worker(db_path, "stale", capability="python", last_seen=old())
    add_worker(db_path, "wrong-project", capability="python", project_scope=2)
    assert plan(db_path)["planned_action"] == "WAIT_EXECUTOR"
    add_worker(db_path, "exec-ok", capability="python", project_scope=1)
    result = plan(db_path, "plan-2")
    assert result["planned_action"] == "CLAIM_TASK"
    assert result["selected_actor_id"] == "exec-ok"


def test_dry_run_zero_writes(db_path):
    add_task(db_path, status="queued")
    add_worker(db_path, "exec-1")
    result = service(db_path).run_one_cycle(1, "cycle-dry", dry_run=True)
    assert result["planned_action"] == "CLAIM_TASK"
    assert count(db_path, "supervisor_cycles") == 0
    assert count(db_path, "task_assignments") == 0
    c = conn(db_path)
    assert c.execute("SELECT status FROM development_tasks WHERE id=10").fetchone()["status"] == "queued"
    c.close()


def test_live_cycle_claims_only_one_task_and_audits_without_token(db_path):
    add_task(db_path, 10, "queued")
    add_task(db_path, 11, "queued")
    add_worker(db_path, "exec-1")
    result = service(db_path).run_one_cycle(1, "cycle-live-claim", dry_run=False)
    assert result["success"] is True
    assert result["planned_action"] == "CLAIM_TASK"
    assert count(db_path, "task_assignments") == 1
    c = conn(db_path)
    assert c.execute("SELECT status FROM development_tasks WHERE id=10").fetchone()["status"] == "claimed"
    assert c.execute("SELECT status FROM development_tasks WHERE id=11").fetchone()["status"] == "queued"
    audit = c.execute("SELECT result_json FROM supervisor_cycles").fetchone()["result_json"]
    assert "lease_token" not in audit
    assert "fingerprint" not in audit
    c.close()


def test_live_cycle_begin_review_once(db_path):
    add_task(db_path, status="result_submitted", version=4)
    add_result(db_path)
    add_worker(db_path, "rev-1", worker_type="reviewer", capability=None)
    result = service(db_path).run_one_cycle(1, "cycle-live-review", dry_run=False)
    assert result["success"] is True
    assert result["planned_action"] == "BEGIN_REVIEW"
    c = conn(db_path)
    assert c.execute("SELECT status, state_version FROM development_tasks WHERE id=10").fetchone()["status"] == "reviewing"
    assert c.execute("SELECT COUNT(*) AS c FROM review_decisions").fetchone()["c"] == 1
    c.close()


def test_same_key_idempotent_after_state_change(db_path):
    add_task(db_path, status="queued")
    add_worker(db_path, "exec-1")
    first = service(db_path).run_one_cycle(1, "cycle-idem", dry_run=False)
    repeat = service(db_path).run_one_cycle(1, "cycle-idem", dry_run=False)
    assert first["success"] is True
    assert repeat["idempotent"] is True
    assert count(db_path, "supervisor_cycles") == 1
    assert count(db_path, "task_assignments") == 1


def test_concurrent_cycle_only_one_claims(db_path):
    add_task(db_path, status="queued")
    add_worker(db_path, "exec-1")
    results = []

    def run(key):
        results.append(service(db_path).run_one_cycle(1, key, dry_run=False))

    threads = [threading.Thread(target=run, args=("cycle-a",)), threading.Thread(target=run, args=("cycle-b",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert count(db_path, "task_assignments") == 1
    assert sum(1 for r in results if r.get("success") and r.get("planned_action") == "CLAIM_TASK") <= 1


def test_no_available_reviewer_returns_clear_result(db_path):
    add_task(db_path, status="result_submitted", version=4)
    add_result(db_path)
    assert plan(db_path)["planned_action"] == "WAIT_REVIEWER"


def test_reviewer_same_as_executor_is_not_selected(db_path):
    add_task(db_path, status="result_submitted", version=4)
    add_result(db_path, worker_id="same-id")
    c = conn(db_path)
    c.execute("UPDATE agent_workers SET worker_type='reviewer', status='available' WHERE worker_id='same-id'")
    c.commit()
    c.close()
    assert plan(db_path)["planned_action"] == "WAIT_REVIEWER"


@pytest.mark.parametrize("status", ["offline", "disabled", "busy"])
def test_unavailable_reviewer_not_selected(db_path, status):
    add_task(db_path, status="result_submitted", version=4)
    add_result(db_path)
    add_worker(db_path, "rev-1", worker_type="reviewer", status=status, capability=None)
    assert plan(db_path)["planned_action"] == "WAIT_REVIEWER"


def test_stale_reviewer_not_selected(db_path):
    add_task(db_path, status="result_submitted", version=4)
    add_result(db_path)
    add_worker(db_path, "rev-1", worker_type="reviewer", capability=None, last_seen=old())
    assert plan(db_path)["planned_action"] == "WAIT_REVIEWER"


def test_result_submitted_without_result_record_waits(db_path):
    add_task(db_path, status="result_submitted", version=4)
    add_worker(db_path, "rev-1", worker_type="reviewer", capability=None)
    assert plan(db_path)["planned_action"] == "WAIT_RESULT_RECORD"


def test_rework_without_executor_waits(db_path):
    add_task(db_path, status="rework", version=5)
    assert plan(db_path)["planned_action"] == "WAIT_EXECUTOR"


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_other_terminal_states_no_action(db_path, state):
    add_task(db_path, status=state, version=8)
    assert plan(db_path)["planned_action"] == "NO_ACTION"


def test_no_project_tasks_no_action(db_path):
    result = plan(db_path)
    assert result["planned_action"] == "NO_ACTION"


def test_feature_flag_false_zero_writes(db_path):
    add_task(db_path, status="queued")
    add_worker(db_path, "exec-1")
    result = service(db_path, enabled=False).run_one_cycle(1, "flag-off", dry_run=False)
    assert result["error_code"] == ERROR_V2_CONTROL_PLANE_DISABLED
    assert count(db_path, "supervisor_cycles") == 0
    assert count(db_path, "task_assignments") == 0


def test_inspect_project_is_read_only(db_path):
    add_task(db_path, status="queued")
    add_worker(db_path, "exec-1")
    result = service(db_path).inspect_project(1)
    assert result["success"] is True
    assert len(result["tasks"]) == 1
    assert count(db_path, "supervisor_cycles") == 0
