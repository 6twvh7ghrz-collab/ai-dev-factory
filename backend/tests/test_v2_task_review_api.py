"""V2.0-B3b review API integration tests."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_v2_task_review import SCHEMA, setup_result


@pytest.fixture
def db_path(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_review_api_")
    os.close(fd)
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO projects VALUES (1, 'p')")
    c.commit()
    c.close()
    setup_result(path)
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


def begin_body(**kwargs):
    body = {
        "action": "begin",
        "result_id": "rslt-1",
        "reviewer_id": "rev-1",
        "expected_version": 4,
    }
    body.update(kwargs)
    return body


def decide_body(**kwargs):
    body = {
        "action": "decide",
        "result_id": "rslt-1",
        "reviewer_id": "rev-1",
        "expected_version": 5,
        "decision": "VERIFIED",
        "summary": "Looks good",
        "issues": [],
        "evidence_refs": ["art-1"],
    }
    body.update(kwargs)
    return body


def test_review_api_normal_200(db_path, client):
    r1 = client.post("/api/v2/tasks/10/review", json=begin_body(), headers={"Idempotency-Key": "api-begin"})
    assert r1.status_code == 200
    assert r1.json()["task_state"] == "REVIEWING"

    r2 = client.post("/api/v2/tasks/10/review", json=decide_body(), headers={"Idempotency-Key": "api-decision"})
    assert r2.status_code == 200
    data = r2.json()
    assert data["decision"] == "VERIFIED"
    assert data["task_state"] == "VERIFIED"
    assert data["state_version"] == 6


def test_review_api_missing_idempotency_key_422(db_path, client):
    resp = client.post("/api/v2/tasks/10/review", json=begin_body())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_review_api_non_reviewer_403(db_path, client):
    c = sqlite3.connect(db_path)
    c.execute("UPDATE agent_workers SET worker_type='executor' WHERE worker_id='rev-1'")
    c.commit()
    c.close()
    resp = client.post("/api/v2/tasks/10/review", json=begin_body(), headers={"Idempotency-Key": "api-non-reviewer"})
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "REVIEWER_TYPE_NOT_ALLOWED"


def test_review_api_version_conflict_409(db_path, client):
    resp = client.post(
        "/api/v2/tasks/10/review",
        json=begin_body(expected_version=2),
        headers={"Idempotency-Key": "api-version"},
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "STATE_VERSION_CONFLICT"


def test_review_api_invalid_decision_422(db_path, client):
    client.post("/api/v2/tasks/10/review", json=begin_body(), headers={"Idempotency-Key": "api-begin2"})
    resp = client.post(
        "/api/v2/tasks/10/review",
        json=decide_body(decision="completed"),
        headers={"Idempotency-Key": "api-invalid-decision"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "DECISION_INVALID"


def test_review_api_response_does_not_leak_sensitive_info(db_path, client):
    client.post("/api/v2/tasks/10/review", json=begin_body(), headers={"Idempotency-Key": "api-begin3"})
    resp = client.post(
        "/api/v2/tasks/10/review",
        json=decide_body(metadata={"DATABASE_URL": "sqlite:///C:/Sandbox/secret.db", "lease_token": "secret"}),
        headers={"Idempotency-Key": "api-leak"},
    )
    raw = resp.text
    assert resp.status_code == 422
    assert "secret.db" not in raw
    assert "lease_token" not in raw
    assert "sqlite" not in raw.lower()
    assert "traceback" not in raw.lower()


def test_review_openapi_only_expected_v2_routes(db_path, client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert paths == {
        "/api/v2/workers/register",
        "/api/v2/tasks/{task_id}/claim",
        "/api/v2/tasks/{task_id}/heartbeat",
        "/api/v2/tasks/{task_id}/submit",
        "/api/v2/tasks/{task_id}/review",
        "/api/v2/tasks/{task_id}/handoff",
    }
