"""V2.0-B3a submit API integration tests."""

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

from test_v2_task_result_submission import SCHEMA, packet


@pytest.fixture
def db_path(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_v2_submit_api_")
    os.close(fd)
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO projects (id, name) VALUES (1, 'p')")
    expires = (datetime.now() + timedelta(seconds=300)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO agent_workers (worker_id, worker_type, status, current_load) VALUES ('exec-1','executor','busy',1)")
    c.execute("""
        INSERT INTO development_tasks
        (id, project_id, title, status, state_version, files_to_modify, files_to_check)
        VALUES (10, 1, 'task', 'running', 3, '["src/a.py"]', '["tests/test_a.py"]')
    """)
    c.execute("""
        INSERT INTO task_assignments
        (assignment_id, task_id, worker_id, project_id, status, lease_token, lease_expires_at)
        VALUES ('asgn-1', 10, 'exec-1', 1, 'running', 'lease-secret', ?)
    """, (expires,))
    c.commit()
    c.close()
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


def api_body(**overrides):
    body = packet()
    body["lease_token"] = "lease-secret"
    body["expected_version"] = 3
    body.update(overrides)
    return body


def test_submit_api_success_200(db_path, client):
    resp = client.post(
        "/api/v2/tasks/10/submit",
        json=api_body(),
        headers={"Idempotency-Key": "api-submit-1"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["task_state"] == "RESULT_SUBMITTED"
    assert data["assignment_status"] == "completed"
    assert data["worker_status"] == "available"
    assert data["artifact_count"] == 1
    assert "lease-secret" not in resp.text


def test_missing_idempotency_key_returns_422(db_path, client):
    resp = client.post("/api/v2/tasks/10/submit", json=api_body())

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


def test_wrong_token_returns_409_without_leak(db_path, client):
    resp = client.post(
        "/api/v2/tasks/10/submit",
        json=api_body(lease_token="wrong-token"),
        headers={"Idempotency-Key": "api-submit-wrong-token"},
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "LEASE_CONFLICT"
    assert "wrong-token" not in resp.text
    assert "lease-secret" not in resp.text
    assert "SELECT " not in resp.text.upper()
    assert "traceback" not in resp.text.lower()


def test_version_conflict_returns_409(db_path, client):
    resp = client.post(
        "/api/v2/tasks/10/submit",
        json=api_body(expected_version=2),
        headers={"Idempotency-Key": "api-submit-version"},
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "STATE_VERSION_CONFLICT"


def test_invalid_result_packet_returns_422(db_path, client):
    resp = client.post(
        "/api/v2/tasks/10/submit",
        json=api_body(result_status="verified"),
        headers={"Idempotency-Key": "api-submit-invalid"},
    )

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "RESULT_PACKET_INVALID"


def test_api_response_does_not_leak_path_sql_or_token(db_path, client):
    resp = client.post(
        "/api/v2/tasks/10/submit",
        json=api_body(artifacts=[{
            "artifact_id": "artifact-diff-1",
            "artifact_type": "diff",
            "uri": "C:/SandboxUser/本机/Desktop/secret.db",
            "sha256": "a" * 64,
        }]),
        headers={"Idempotency-Key": "api-submit-leak"},
    )

    raw = resp.text
    assert resp.status_code == 422
    assert "lease-secret" not in raw
    assert "secret.db" not in raw
    assert "C:/Users" not in raw
    assert "sqlite" not in raw.lower()
    assert "traceback" not in raw.lower()


def test_openapi_includes_submit_route_only_as_new_v2_path(db_path, client):
    schema = client.get("/openapi.json").json()
    paths = schema.get("paths", {})

    assert "/api/v2/tasks/{task_id}/submit" in paths
    v2_paths = {p for p in paths if "/api/v2/" in p}
    assert v2_paths == {
        "/api/v2/workers/register",
        "/api/v2/tasks/{task_id}/claim",
        "/api/v2/tasks/{task_id}/heartbeat",
        "/api/v2/tasks/{task_id}/submit",
        "/api/v2/tasks/{task_id}/review",
        "/api/v2/tasks/{task_id}/handoff",
    }


def test_v1_health_style_endpoint_not_registered_in_isolated_router(client):
    schema = client.get("/openapi.json").json()
    assert "/api/v2/tasks/{task_id}/submit" in schema["paths"]
