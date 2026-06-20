"""V2.0-B3b TaskReviewService tests."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.supervisor.task_review_service import (
    TaskReviewService,
    ERROR_DECISION_INVALID,
    ERROR_EVIDENCE_INVALID,
    ERROR_REVIEWER_NOT_AVAILABLE,
    ERROR_REVIEWER_TYPE_NOT_ALLOWED,
    ERROR_RESULT_NOT_FOUND,
    ERROR_STATE_VERSION_CONFLICT,
    ERROR_TASK_NOT_REVIEWABLE,
    ERROR_V2_CONTROL_PLANE_DISABLED,
)


SCHEMA = """
CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE development_tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 1,
    last_state_change TEXT
);
CREATE TABLE agent_workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL UNIQUE,
    worker_type TEXT NOT NULL CHECK (worker_type IN ('executor','supervisor','reviewer')),
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('registered','available','busy','offline','disabled'))
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
"""


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_review_")
    os.close(fd)
    c = sqlite3.connect(path)
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


def setup_result(path, *, task_status="result_submitted", version=4, reviewer_type="reviewer",
                 reviewer_status="available", tests_failed=0):
    c = conn(path)
    c.execute("INSERT INTO development_tasks VALUES (10, 1, ?, ?, NULL)", (task_status, version))
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status) VALUES ('exec-1','executor','available')")
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status) VALUES ('rev-1', ?, ?)", (reviewer_type, reviewer_status))
    c.execute("""
        INSERT INTO task_results
        (result_id, task_id, assignment_id, worker_id, project_id, result_status,
         tests_total, tests_passed, tests_failed, tests_skipped, evidence_refs_json)
        VALUES ('rslt-1', 10, 'asgn-1', 'exec-1', 1, 'submitted', 2, ?, ?, 0, '["art-1"]')
    """, (2 - tests_failed, tests_failed))
    c.execute("""
        INSERT INTO execution_artifacts
        (artifact_id, result_id, task_id, assignment_id, project_id, artifact_type, storage_path)
        VALUES ('art-1', 'rslt-1', 10, 'asgn-1', 1, 'test_report', 'artifacts/report.txt')
    """)
    c.commit()
    c.close()


def service(path, enabled=True):
    return TaskReviewService(path, v2_enabled=enabled)


def begin(path, **kwargs):
    return service(path).begin_review(
        kwargs.get("task_id", 10),
        kwargs.get("result_id", "rslt-1"),
        kwargs.get("reviewer_id", "rev-1"),
        kwargs.get("expected_version", 4),
        kwargs.get("idempotency_key", "begin-1"),
    )


def decide(path, **kwargs):
    return service(path).submit_decision(
        kwargs.get("task_id", 10),
        kwargs.get("result_id", "rslt-1"),
        kwargs.get("reviewer_id", "rev-1"),
        kwargs.get("expected_version", 5),
        kwargs.get("decision", "VERIFIED"),
        kwargs.get("summary", "Looks good"),
        kwargs.get("issues", []),
        kwargs.get("evidence_refs", ["art-1"]),
        kwargs.get("idempotency_key", "decision-1"),
        kwargs.get("risk_level", "low"),
        kwargs.get("user_action_required", False),
        kwargs.get("metadata", {}),
    )


def count(path, table):
    c = conn(path)
    try:
        return c.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        c.close()


def test_begin_review_success_writes_review_event_and_updates_task(db_path):
    setup_result(db_path)
    result = begin(db_path)
    assert result["success"] is True
    assert result["task_state"] == "REVIEWING"
    assert result["state_version"] == 5
    c = conn(db_path)
    assert c.execute("SELECT decision FROM review_decisions").fetchone()["decision"] == "REVIEWING"
    assert c.execute("SELECT status FROM development_tasks WHERE id=10").fetchone()["status"] == "reviewing"
    assert c.execute("SELECT event_type FROM task_events").fetchone()["event_type"] == "review"
    c.close()


@pytest.mark.parametrize("worker_type,code", [
    ("executor", ERROR_REVIEWER_TYPE_NOT_ALLOWED),
    ("supervisor", ERROR_REVIEWER_TYPE_NOT_ALLOWED),
])
def test_non_reviewer_rejected(db_path, worker_type, code):
    setup_result(db_path, reviewer_type=worker_type)
    assert begin(db_path)["error_code"] == code


@pytest.mark.parametrize("status", ["offline", "disabled"])
def test_unavailable_reviewer_rejected(db_path, status):
    setup_result(db_path, reviewer_status=status)
    assert begin(db_path)["error_code"] == ERROR_REVIEWER_NOT_AVAILABLE


def test_result_missing_and_task_state_and_version_conflict(db_path):
    setup_result(db_path)
    assert begin(db_path, result_id="missing")["error_code"] == ERROR_RESULT_NOT_FOUND
    c = conn(db_path)
    c.execute("UPDATE development_tasks SET status='running'")
    c.commit()
    c.close()
    assert begin(db_path)["error_code"] == ERROR_TASK_NOT_REVIEWABLE
    c = conn(db_path)
    c.execute("UPDATE development_tasks SET status='result_submitted', state_version=9")
    c.commit()
    c.close()
    assert begin(db_path)["error_code"] == ERROR_STATE_VERSION_CONFLICT


def test_begin_idempotent(db_path):
    setup_result(db_path)
    first = begin(db_path)
    second = begin(db_path)
    assert first["success"] is True
    assert second["success"] is True
    assert second["idempotent"] is True
    assert count(db_path, "review_decisions") == 1
    assert count(db_path, "task_events") == 1


def test_concurrent_begin_only_one_success(db_path):
    setup_result(db_path)
    results = []
    t1 = threading.Thread(target=lambda: results.append(begin(db_path, reviewer_id="rev-1", idempotency_key="b1")))
    c = conn(db_path)
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status) VALUES ('rev-2','reviewer','available')")
    c.commit()
    c.close()
    t2 = threading.Thread(target=lambda: results.append(begin(db_path, reviewer_id="rev-2", idempotency_key="b2")))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sum(1 for r in results if r.get("success")) == 1
    assert count(db_path, "review_decisions") == 1


def test_verified_success_does_not_write_completed(db_path):
    setup_result(db_path)
    begin(db_path)
    result = decide(db_path)
    assert result["success"] is True
    assert result["task_state"] == "VERIFIED"
    c = conn(db_path)
    assert c.execute("SELECT status FROM development_tasks WHERE id=10").fetchone()["status"] == "verified"
    assert c.execute("SELECT decision FROM review_decisions").fetchone()["decision"] == "VERIFIED"
    c.close()


def test_verified_requires_summary_evidence_and_no_unexplained_failures(db_path):
    setup_result(db_path, tests_failed=1)
    begin(db_path)
    assert decide(db_path, summary="")["error_code"] == ERROR_DECISION_INVALID
    assert decide(db_path, evidence_refs=["missing"], idempotency_key="d2")["error_code"] == ERROR_EVIDENCE_INVALID
    assert decide(db_path, idempotency_key="d3")["error_code"] == ERROR_DECISION_INVALID


def test_verified_rejects_failed_tests_even_with_issue_explanation(db_path):
    setup_result(db_path, tests_failed=1)
    begin(db_path)
    issue = {"severity": "high", "reason": "known failure", "acceptance": "fix before pass"}
    result = decide(db_path, issues=[issue], idempotency_key="d4")
    assert result["error_code"] == ERROR_DECISION_INVALID


def test_rework_requires_complete_issues_and_succeeds(db_path):
    setup_result(db_path)
    begin(db_path)
    assert decide(db_path, decision="REWORK", issues=[], idempotency_key="rw0")["error_code"] == ERROR_DECISION_INVALID
    issue = {"severity": "high", "file": "src/a.py", "reason": "bug", "acceptance": "test passes"}
    result = decide(db_path, decision="REWORK", issues=[issue], summary="Needs rework", idempotency_key="rw1")
    assert result["success"] is True
    assert result["task_state"] == "REWORK"


def test_blocked_and_need_user_rules(db_path):
    setup_result(db_path)
    begin(db_path)
    assert decide(db_path, decision="BLOCKED", issues=[], idempotency_key="blk0")["error_code"] == ERROR_DECISION_INVALID
    issue = {"severity": "medium", "reason": "dependency unavailable", "acceptance": "dependency restored"}
    assert decide(db_path, decision="BLOCKED", issues=[issue], summary="Blocked", idempotency_key="blk1")["success"] is True

    setup_result(db_path + ".x") if False else None


def test_need_user_success(db_path):
    setup_result(db_path)
    begin(db_path)
    issue = {"question": "Accept scope change?", "options": ["yes", "no"], "risk": "may delay"}
    result = decide(db_path, decision="NEED_USER", issues=[issue], summary="Needs user", idempotency_key="nu1", user_action_required=True)
    assert result["success"] is True
    assert result["task_state"] == "NEED_USER"


def test_decision_idempotency_and_conflict(db_path):
    setup_result(db_path)
    begin(db_path)
    first = decide(db_path)
    second = decide(db_path)
    assert second["success"] is True
    assert second["idempotent"] is True
    assert second["decision_id"] == first["decision_id"]
    assert count(db_path, "task_events") == 2
    assert decide(db_path, summary="changed")["error_code"] == "IDEMPOTENCY_CONFLICT"


def test_terminal_cannot_be_decided_again(db_path):
    setup_result(db_path)
    begin(db_path)
    assert decide(db_path)["success"] is True
    assert decide(db_path, idempotency_key="new", expected_version=6, decision="REWORK", issues=[{"severity": "high", "reason": "x", "acceptance": "y"}])["error_code"] == ERROR_TASK_NOT_REVIEWABLE


def test_flag_false_writes_nothing(db_path):
    setup_result(db_path)
    result = service(db_path, enabled=False).begin_review(10, "rslt-1", "rev-1", 4, "off")
    assert result["error_code"] == ERROR_V2_CONTROL_PLANE_DISABLED
    assert count(db_path, "review_decisions") == 0
    assert count(db_path, "task_events") == 0
