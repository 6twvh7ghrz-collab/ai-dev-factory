"""V2.0-B4 handoff API integration tests."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_v2_task_handoff import SCHEMA, setup_running


@pytest.fixture
def db_path(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_handoff_api_")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA)
    c.execute("INSERT INTO projects VALUES (1, 'p')")
    c.commit()
    c.close()
    setup_running(path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
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


@pytest.fixture
def client():
    from app.api.v2_worker_api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def request_body(**kw):
    body = {
        "action": "request",
        "assignment_id": "asgn-1",
        "from_worker_id": "exec-1",
        "lease_token": "lease-secret",
        "reason_code": "CAPABILITY_MISMATCH",
        "reason": "needs another executor",
        "completed_steps": ["read code"],
        "remaining_steps": ["finish tests"],
        "files_changed": ["backend/app/a.py"],
        "tests_run": [{"cmd": "pytest", "status": "failed"}],
        "recent_errors": [],
        "evidence_refs": [],
        "forbidden_actions": ["do not run executor"],
        "context_snapshot": {"note": "safe"},
        "git_head": "a" * 40,
    }
    body.update(kw)
    return body


def make_handoff(client):
    resp = client.post("/api/v2/tasks/10/handoff", json=request_body(), headers={"Idempotency-Key": "api-request"})
    assert resp.status_code == 200
    return resp.json()["handoff_id"]


def test_handoff_api_request_accept_reject_cancel(db_path, client):
    hid = make_handoff(client)
    accept = client.post(
        "/api/v2/tasks/10/handoff",
        json={"action": "accept", "handoff_id": hid, "to_worker_id": "exec-2", "expected_version": 3},
        headers={"Idempotency-Key": "api-accept"},
    )
    assert accept.status_code == 200
    data = accept.json()
    assert data["status"] == "accepted"
    assert data["assignment_id"]
    assert "lease-secret" not in accept.text
    assert "lease_token" not in accept.text

    c = sqlite3.connect(db_path)
    c.execute("UPDATE task_handoffs SET status='pending', to_worker_id='exec-2' WHERE handoff_id=?", (hid,))
    c.commit()
    c.close()
    reject = client.post(
        "/api/v2/tasks/10/handoff",
        json={"action": "reject", "handoff_id": hid, "worker_id": "exec-2", "reason": "no capacity"},
        headers={"Idempotency-Key": "api-reject"},
    )
    assert reject.status_code == 200
    assert reject.json()["status"] == "rejected"

    c = sqlite3.connect(db_path)
    c.execute("UPDATE task_assignments SET status='running' WHERE assignment_id='asgn-1'")
    c.execute("UPDATE agent_workers SET status='busy', current_load=1 WHERE worker_id='exec-1'")
    c.commit()
    c.close()
    hid2 = client.post("/api/v2/tasks/10/handoff", json=request_body(), headers={"Idempotency-Key": "api-request-2"}).json()["handoff_id"]
    cancel = client.post(
        "/api/v2/tasks/10/handoff",
        json={"action": "cancel", "handoff_id": hid2, "actor_id": "exec-1", "reason": "cancel"},
        headers={"Idempotency-Key": "api-cancel"},
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"


def test_handoff_api_missing_idempotency_key_returns_422(db_path, client):
    resp = client.post("/api/v2/tasks/10/handoff", json=request_body())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_handoff_api_conflict_and_not_found(db_path, client):
    hid = make_handoff(client)
    first = client.post(
        "/api/v2/tasks/10/handoff",
        json={"action": "accept", "handoff_id": hid, "to_worker_id": "exec-2", "expected_version": 3},
        headers={"Idempotency-Key": "api-accept-conflict"},
    )
    second = client.post(
        "/api/v2/tasks/10/handoff",
        json={"action": "accept", "handoff_id": hid, "to_worker_id": "exec-2", "expected_version": 3},
        headers={"Idempotency-Key": "api-accept-other"},
    )
    missing = client.post(
        "/api/v2/tasks/10/handoff",
        json={"action": "accept", "handoff_id": "missing", "to_worker_id": "exec-2", "expected_version": 3},
        headers={"Idempotency-Key": "api-missing"},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert missing.status_code == 404


def test_handoff_api_response_does_not_leak_sensitive_info(db_path, client):
    resp = client.post(
        "/api/v2/tasks/10/handoff",
        json=request_body(files_changed=["C:/SandboxUser/local/secret.db"], context_snapshot={"DATABASE_URL": "sqlite:///C:/SandboxUser/local/secret.db"}),
        headers={"Idempotency-Key": "api-leak"},
    )
    raw = resp.text
    assert resp.status_code == 422
    assert "lease-secret" not in raw
    assert "secret.db" not in raw
    assert "C:/Users" not in raw
    assert "sqlite" not in raw.lower()
    assert "traceback" not in raw.lower()


def test_handoff_api_expire(db_path, client):
    hid = make_handoff(client)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE task_handoffs SET expires_at=? WHERE handoff_id=?", ((datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), hid))
    c.commit()
    c.close()
    resp = client.post("/api/v2/tasks/10/handoff", json={"action": "expire"}, headers={"Idempotency-Key": "api-expire"})
    assert resp.status_code == 200
    assert resp.json()["expired_count"] == 1


def test_handoff_openapi_only_adds_expected_route(db_path, client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert paths == {
        "/api/v2/workers/register",
        "/api/v2/tasks/{task_id}/claim",
        "/api/v2/tasks/{task_id}/heartbeat",
        "/api/v2/tasks/{task_id}/submit",
        "/api/v2/tasks/{task_id}/review",
        "/api/v2/tasks/{task_id}/handoff",
    }
