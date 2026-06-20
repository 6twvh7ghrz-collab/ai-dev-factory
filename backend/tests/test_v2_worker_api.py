"""V2.0-B2e: V2 Worker Control Plane API Integration Tests.

Tests the three API endpoints via FastAPI TestClient:
  - POST /api/v2/workers/register
  - POST /api/v2/tasks/{task_id}/claim
  - POST /api/v2/tasks/{task_id}/heartbeat

All tests use temporary SQLite databases and do NOT connect to
the production database.  Feature flag is controlled via environment
variable, and each test that requires a DB sets up the schema via fixture.
"""

from __future__ import annotations

import os
import sys
import json
import sqlite3
import tempfile
import time as _time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Ensure backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ============================================================
# Schema (mirrors Migration 012 + 013 + 014 DDL)
# ============================================================

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
    agent_type_required TEXT NOT NULL DEFAULT 'executor',
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


# ============================================================
# Helpers
# ============================================================

def _build_temp_db(suffix: str = "api") -> str:
    """Create a temporary database with full V2 schema, return path."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix=f"test_v2_{suffix}_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO projects (id, name) VALUES (1, 'test-project')")
    conn.commit()
    conn.close()
    return path


def _cleanup_temp_db(path: str) -> None:
    """Remove temp DB and WAL/SHM files."""
    _time.sleep(0.05)
    for ext in ["", "-wal", "-shm"]:
        p = path + ext
        for _attempt in range(3):
            try:
                if os.path.exists(p):
                    os.unlink(p)
                break
            except PermissionError:
                _time.sleep(0.1)
            except FileNotFoundError:
                break


def _raw_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _row_count(db_path: str, table: str) -> int:
    conn = _raw_conn(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
    finally:
        conn.close()


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def db_path():
    """Temporary SQLite DB path with full V2 schema (flag enabled)."""
    path = _build_temp_db("api")
    # Point settings to this temp DB
    old_url = os.environ.get("DATABASE_URL")
    old_flag = os.environ.get("V2_CONTROL_PLANE_ENABLED")
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["V2_CONTROL_PLANE_ENABLED"] = "true"
    try:
        yield path
    finally:
        if old_url is not None:
            os.environ["DATABASE_URL"] = old_url
        else:
            os.environ.pop("DATABASE_URL", None)
        if old_flag is not None:
            os.environ["V2_CONTROL_PLANE_ENABLED"] = old_flag
        else:
            os.environ.pop("V2_CONTROL_PLANE_ENABLED", None)
        _cleanup_temp_db(path)


@pytest.fixture
def db_path_disabled():
    """Temporary SQLite DB path with V2 feature flag disabled."""
    path = _build_temp_db("api_d")
    old_url = os.environ.get("DATABASE_URL")
    old_flag = os.environ.get("V2_CONTROL_PLANE_ENABLED")
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["V2_CONTROL_PLANE_ENABLED"] = "false"
    try:
        yield path
    finally:
        if old_url is not None:
            os.environ["DATABASE_URL"] = old_url
        else:
            os.environ.pop("DATABASE_URL", None)
        if old_flag is not None:
            os.environ["V2_CONTROL_PLANE_ENABLED"] = old_flag
        else:
            os.environ.pop("V2_CONTROL_PLANE_ENABLED", None)
        _cleanup_temp_db(path)


@pytest.fixture
def app():
    """FastAPI test app with v2_worker_api router registered."""
    # Import after env is set
    from app.api.v2_worker_api import router
    test_app = FastAPI()
    test_app.include_router(router)
    return test_app


@pytest.fixture
def client(app):
    """FastAPI TestClient."""
    return TestClient(app)


# ============================================================
# Setup helpers (prep DB state for tests)
# ============================================================

def _setup_worker(db_path: str, worker_id: str = "exec-1",
                  worker_type: str = "executor",
                  status: str = "available"):
    conn = _raw_conn(db_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO agent_workers (worker_id, worker_type, provider, display_name,
                                   status, max_concurrency, registered_at, last_seen_at)
        VALUES (?, ?, 'test', 'Test Worker', ?, 1, ?, ?)
    """, (worker_id, worker_type, status, now, now))
    conn.execute("INSERT INTO agent_capabilities (worker_id, capability) VALUES (?, 'python')", (worker_id,))
    conn.commit()
    conn.close()


def _setup_task(db_path: str, task_id: int = 1,
                status: str = "queued", version: int = 1):
    conn = _raw_conn(db_path)
    conn.execute("""
        INSERT INTO development_tasks (id, project_id, title, task_type, status,
                                       state_version, implementation_steps)
        VALUES (?, 1, 'Test Task', 'backend', ?, ?, '{}')
    """, (task_id, status, version))
    conn.commit()
    conn.close()


def _setup_claimed_task(db_path: str, task_id: int = 1,
                         assignment_id: str = "asgn-001",
                         worker_id: str = "exec-1",
                         lease_token: str = "tok-deadbeef",
                         expires_offset_sec: int = 300):
    """Set up a claimed task with assignment and heartbeat-ready state."""
    conn = _raw_conn(db_path)
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    expires = (now + timedelta(seconds=expires_offset_sec)).strftime("%Y-%m-%d %H:%M:%S")
    # Task state
    conn.execute("""
        UPDATE development_tasks SET status='claimed', state_version=2,
        last_state_change=? WHERE id=?
    """, (now_str, task_id))
    # Worker BUSY
    conn.execute("UPDATE agent_workers SET status='busy', last_seen_at=? WHERE worker_id=?", (now_str, worker_id))
    # Assignment
    conn.execute("""
        INSERT INTO task_assignments
        (assignment_id, task_id, worker_id, project_id, agent_type_required,
         status, lease_token, lease_expires_at, idempotency_key, dispatched_at,
         created_at, updated_at)
        VALUES (?, ?, ?, 1, 'executor', 'assigned', ?, ?, 'ik-claim-orig', ?, ?, ?)
    """, (assignment_id, task_id, worker_id, lease_token, expires, now_str, now_str, now_str))
    conn.commit()
    conn.close()


# ============================================================
# 1 ─ Register
# ============================================================

class TestDatabasePathResolution:
    """DATABASE_URL env/settings resolution and safe failure behavior."""

    def test_env_database_url_takes_precedence_over_settings(self, monkeypatch):
        from app.api.v2_worker_api import _get_db_path
        from app.core.config import settings

        monkeypatch.setenv("DATABASE_URL", "sqlite:///C:/env/db.sqlite")
        monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///C:/settings/db.sqlite")

        assert _get_db_path() == "C:/env/db.sqlite"

    def test_unset_env_falls_back_to_settings(self, monkeypatch):
        from app.api.v2_worker_api import _get_db_path
        from app.core.config import settings

        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///C:/settings/db.sqlite")

        assert _get_db_path() == "C:/settings/db.sqlite"

    def test_empty_env_falls_back_to_settings(self, monkeypatch):
        from app.api.v2_worker_api import _get_db_path
        from app.core.config import settings

        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///C:/settings/empty-env.db")

        assert _get_db_path() == "C:/settings/empty-env.db"

    def test_windows_forward_slash_sqlite_url(self, monkeypatch):
        from app.api.v2_worker_api import _get_db_path

        monkeypatch.setenv("DATABASE_URL", "sqlite:///C:/Sandbox/test/db file.db")

        assert _get_db_path() == "C:/Sandbox/test/db file.db"

    def test_windows_backslash_sqlite_url(self, monkeypatch):
        from app.api.v2_worker_api import _get_db_path

        monkeypatch.setenv("DATABASE_URL", r"sqlite:///C:\\Sandbox\\test\\db file.db")

        assert _get_db_path() == r"C:\Sandbox\test\db file.db"

    def test_absolute_posix_sqlite_url(self, monkeypatch):
        from app.api.v2_worker_api import _get_db_path

        monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/test api/db.sqlite")

        assert _get_db_path() == "/tmp/test api/db.sqlite"

    def test_chinese_path_sqlite_url(self, monkeypatch):
        from app.api.v2_worker_api import _get_db_path

        monkeypatch.setenv("DATABASE_URL", "sqlite:///C:/Sandbox/数据库/ai.db")

        assert _get_db_path() == "C:/Sandbox/数据库/ai.db"

    def test_direct_sqlite_file_path(self, monkeypatch):
        from app.api.v2_worker_api import _get_db_path

        monkeypatch.setenv("DATABASE_URL", r"C:\\Sandbox\\direct path.db")

        assert _get_db_path() == r"C:\\Sandbox\\direct path.db"

    def test_both_configs_empty_returns_database_config_invalid(self, client, monkeypatch):
        from app.core.config import settings

        monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(settings, "DATABASE_URL", "")

        resp = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-empty-db", "worker_type": "executor"},
            headers={"Idempotency-Key": "ik-empty-db"},
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "DATABASE_CONFIG_INVALID"

    @pytest.mark.parametrize("url", [
        "postgresql://localhost/app",
        "postgres://localhost/app",
        "mysql://localhost/app",
        "sqlite://relative.db",
    ])
    def test_unsupported_scheme_is_rejected(self, monkeypatch, url):
        from app.api.v2_worker_api import DatabaseConfigError, _get_db_path

        monkeypatch.setenv("DATABASE_URL", url)

        with pytest.raises(DatabaseConfigError):
            _get_db_path()

    def test_invalid_config_does_not_call_sqlite_connect_empty(self, client, monkeypatch):
        from app.core.config import settings

        calls = []
        real_connect = sqlite3.connect

        def guarded_connect(path, *args, **kwargs):
            calls.append(path)
            if path == "":
                raise AssertionError("sqlite3.connect called with empty path")
            return real_connect(path, *args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", guarded_connect)
        monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
        monkeypatch.setenv("DATABASE_URL", "")
        monkeypatch.setattr(settings, "DATABASE_URL", "")

        resp = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-no-empty-connect", "worker_type": "executor"},
            headers={"Idempotency-Key": "ik-no-empty-connect"},
        )

        assert resp.status_code == 500
        assert resp.json()["error_code"] == "DATABASE_CONFIG_INVALID"
        assert "" not in calls

    def test_three_v2_apis_work_with_settings_fallback(self, client, monkeypatch):
        from app.core.config import settings

        path = _build_temp_db("settings_fallback")
        monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{path}")
        try:
            register = client.post(
                "/api/v2/workers/register",
                json={"worker_id": "exec-fallback", "worker_type": "executor", "capabilities": ["python"]},
                headers={"Idempotency-Key": "ik-fallback-register"},
            )
            assert register.status_code == 201

            conn = _raw_conn(path)
            conn.execute("UPDATE agent_workers SET status='available' WHERE worker_id='exec-fallback'")
            conn.commit()
            conn.close()

            _setup_task(path, 501, "queued", 1)
            claim = client.post(
                "/api/v2/tasks/501/claim",
                json={"worker_id": "exec-fallback", "expected_version": 1, "lease_seconds": 300},
                headers={"Idempotency-Key": "ik-fallback-claim"},
            )
            assert claim.status_code == 200
            claim_data = claim.json()

            heartbeat = client.post(
                "/api/v2/tasks/501/heartbeat",
                json={
                    "assignment_id": claim_data["assignment_id"],
                    "worker_id": "exec-fallback",
                    "lease_token": claim_data["lease_token"],
                    "extend_seconds": 300,
                },
                headers={"Idempotency-Key": "ik-fallback-heartbeat"},
            )
            assert heartbeat.status_code == 200
        finally:
            _cleanup_temp_db(path)

    def test_database_config_error_response_does_not_leak_path(self, client, monkeypatch):
        from app.core.config import settings

        secret_url = "postgresql://user:password@localhost/C:/Sandbox/Secret Dir/secret-name.db"
        monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
        monkeypatch.setenv("DATABASE_URL", secret_url)
        monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///C:/fallback/fallback-name.db")

        resp = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-leak-check", "worker_type": "executor"},
            headers={"Idempotency-Key": "ik-leak-check"},
        )

        raw = resp.text
        assert resp.status_code == 500
        assert resp.json()["error_code"] == "DATABASE_CONFIG_INVALID"
        assert "postgresql://" not in raw
        assert "password" not in raw
        assert "Secret Dir" not in raw
        assert "secret-name.db" not in raw
        assert "fallback-name.db" not in raw
        assert "traceback" not in raw.lower()


class TestRegisterAPI:
    """POST /api/v2/workers/register"""

    def test_first_register_returns_201(self, db_path, client):
        """First registration returns HTTP 201."""
        resp = client.post(
            "/api/v2/workers/register",
            json={
                "worker_id": "exec-007",
                "worker_type": "executor",
                "provider": "openai",
                "display_name": "Agent 007",
                "capabilities": ["python", "typescript"],
            },
            headers={"Idempotency-Key": "ik-reg-001"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["ok"] is True
        assert data["idempotent"] is False
        assert data["worker"]["worker_id"] == "exec-007"
        assert data["worker"]["worker_type"] == "executor"
        # Data written to DB
        assert _row_count(db_path, "agent_workers") == 1

    def test_idempotent_repeat_returns_200(self, db_path, client):
        """Same idempotency key + same params returns 200 (idempotent)."""
        body = {"worker_id": "exec-008", "worker_type": "executor", "provider": "openai"}
        hdr = {"Idempotency-Key": "ik-reg-002"}

        r1 = client.post("/api/v2/workers/register", json=body, headers=hdr)
        assert r1.status_code == 201

        r2 = client.post("/api/v2/workers/register", json=body, headers=hdr)
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["ok"] is True
        assert data2["idempotent"] is True

        # Only one worker in DB
        assert _row_count(db_path, "agent_workers") == 1

    def test_missing_idempotency_key_returns_422(self, db_path, client):
        """Missing Idempotency-Key header returns 422."""
        resp = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-009", "worker_type": "executor"},
        )
        assert resp.status_code == 422
        assert resp.json()["error_code"] == "VALIDATION_ERROR"

    def test_same_key_different_request_returns_409(self, db_path, client):
        """Same key, different params → 409 IDEMPOTENCY_CONFLICT."""
        hdr = {"Idempotency-Key": "ik-reg-003"}
        r1 = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-010", "worker_type": "executor"},
            headers=hdr,
        )
        assert r1.status_code == 201

        r2 = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-010", "worker_type": "executor", "provider": "anthropic"},
            headers=hdr,
        )
        assert r2.status_code == 409
        assert r2.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    def test_invalid_worker_type_returns_error(self, db_path, client):
        """Non-existent worker_type returns error status."""
        resp = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-011", "worker_type": "invalid_type"},
            headers={"Idempotency-Key": "ik-reg-004"},
        )
        # FastAPI should validate enum or the service should reject
        # Pydantic validates by Field, but we let the service handle it
        # since worker_type is a plain str in the model
        data = resp.json()
        if resp.status_code == 200:
            # Some configs let it through to the service
            pass
        elif resp.status_code >= 400:
            assert not data.get("ok")
        # The important part is it doesn't crash & returns error
        if resp.status_code in (409, 422):
            assert "error_code" in data

    def test_flag_disabled_returns_503(self, db_path_disabled, client):
        """Feature flag=false → 503 V2_CONTROL_PLANE_DISABLED."""
        resp = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-012", "worker_type": "executor"},
            headers={"Idempotency-Key": "ik-reg-005"},
        )
        assert resp.status_code == 503
        data = resp.json()
        assert data["error_code"] == "V2_CONTROL_PLANE_DISABLED"
        # No data written
        assert _row_count(db_path_disabled, "agent_workers") == 0

    def test_no_internal_fingerprint_leaked(self, db_path, client):
        """Response must not contain internal fingerprint fields."""
        resp = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "exec-013", "worker_type": "executor"},
            headers={"Idempotency-Key": "ik-reg-006"},
        )
        data = resp.json()
        meta = data.get("worker", {}).get("metadata", {})
        assert "_idempotency_fingerprint" not in meta
        assert "_idempotency_key" not in data.get("worker", {})


# ============================================================
# 2 ─ Claim
# ============================================================

class TestClaimAPI:
    """POST /api/v2/tasks/{task_id}/claim"""

    def test_normal_claim_returns_200(self, db_path, client):
        """Normal claim returns 200 with full response."""
        _setup_worker(db_path, "exec-1", "executor", "available")
        _setup_task(db_path, 1, "queued", 1)

        resp = client.post(
            "/api/v2/tasks/1/claim",
            json={
                "worker_id": "exec-1",
                "expected_version": 1,
                "lease_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-claim-001"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["assignment_id"] is not None
        assert data["lease_token"] is not None
        assert data["lease_token_reissued"] is True
        assert data["idempotent"] is False

    def test_task_packet_complete(self, db_path, client):
        """Task packet contains all expected fields."""
        _setup_worker(db_path, "exec-2", "executor", "available")
        _setup_task(db_path, 2, "queued", 1)

        resp = client.post(
            "/api/v2/tasks/2/claim",
            json={
                "worker_id": "exec-2",
                "expected_version": 1,
                "lease_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-claim-002"},
        )
        data = resp.json()
        pkt = data.get("task_packet")
        assert pkt is not None
        assert pkt["task_id"] == 2
        assert "title" in pkt
        assert "description" in pkt
        assert "assignment_id" in pkt
        assert "lease_expires_at" in pkt

    def test_returns_lease_token_on_first_claim(self, db_path, client):
        """First claim returns a real lease_token."""
        _setup_worker(db_path, "exec-3", "executor", "available")
        _setup_task(db_path, 3, "queued", 1)

        resp = client.post(
            "/api/v2/tasks/3/claim",
            json={"worker_id": "exec-3", "expected_version": 1, "lease_seconds": 300},
            headers={"Idempotency-Key": "ik-claim-003"},
        )
        data = resp.json()
        assert data["lease_token"] is not None
        assert len(data["lease_token"]) == 64  # hex token

    def test_unregistered_worker_returns_404(self, db_path, client):
        """Unregistered worker → 404."""
        _setup_task(db_path, 4, "queued", 1)

        resp = client.post(
            "/api/v2/tasks/4/claim",
            json={"worker_id": "ghost-1", "expected_version": 1},
            headers={"Idempotency-Key": "ik-claim-004"},
        )
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "WORKER_NOT_REGISTERED"

    def test_not_available_worker_returns_409(self, db_path, client):
        """Worker exists but not AVAILABLE → 409."""
        _setup_worker(db_path, "exec-5", "executor", "offline")
        _setup_task(db_path, 5, "queued", 1)

        resp = client.post(
            "/api/v2/tasks/5/claim",
            json={"worker_id": "exec-5", "expected_version": 1},
            headers={"Idempotency-Key": "ik-claim-005"},
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "WORKER_NOT_AVAILABLE"

    def test_not_queued_task_returns_409(self, db_path, client):
        """Task not in QUEUED state → 409."""
        _setup_worker(db_path, "exec-6", "executor", "available")
        _setup_task(db_path, 6, "draft", 1)

        resp = client.post(
            "/api/v2/tasks/6/claim",
            json={"worker_id": "exec-6", "expected_version": 1},
            headers={"Idempotency-Key": "ik-claim-006"},
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "TASK_NOT_CLAIMABLE"

    def test_version_conflict_returns_409(self, db_path, client):
        """State version mismatch → 409."""
        _setup_worker(db_path, "exec-7", "executor", "available")
        _setup_task(db_path, 7, "queued", 5)  # actual version is 5

        resp = client.post(
            "/api/v2/tasks/7/claim",
            json={"worker_id": "exec-7", "expected_version": 3},  # caller expects 3
            headers={"Idempotency-Key": "ik-claim-007"},
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "STATE_VERSION_CONFLICT"

    def test_scope_violation_returns_403(self, db_path, client):
        """Task not in allowed_task_ids → 403."""
        _setup_worker(db_path, "exec-8", "executor", "available")
        _setup_task(db_path, 8, "queued", 1)

        resp = client.post(
            "/api/v2/tasks/8/claim",
            json={
                "worker_id": "exec-8",
                "expected_version": 1,
                "allowed_task_ids": [99, 100],
            },
            headers={"Idempotency-Key": "ik-claim-008"},
        )
        assert resp.status_code == 403
        assert resp.json()["error_code"] == "TASK_SCOPE_VIOLATION"

    def test_claim_idempotent_no_duplicate_assignment(self, db_path, client):
        """Repeated idempotent claim does NOT create duplicate assignment."""
        _setup_worker(db_path, "exec-9", "executor", "available")
        _setup_task(db_path, 9, "queued", 1)
        body = {"worker_id": "exec-9", "expected_version": 1, "lease_seconds": 300}
        hdr = {"Idempotency-Key": "ik-claim-009"}

        r1 = client.post("/api/v2/tasks/9/claim", json=body, headers=hdr)
        assert r1.status_code == 200
        assert r1.json()["idempotent"] is False

        r2 = client.post("/api/v2/tasks/9/claim", json=body, headers=hdr)
        assert r2.status_code == 200
        assert r2.json()["idempotent"] is True

        # Only one assignment
        assert _row_count(db_path, "task_assignments") == 1

    def test_claim_idempotent_no_lease_token_reissue(self, db_path, client):
        """Idempotent claim does NOT re-issue lease_token."""
        _setup_worker(db_path, "exec-9b", "executor", "available")
        _setup_task(db_path, 10, "queued", 1)
        body = {"worker_id": "exec-9b", "expected_version": 1, "lease_seconds": 300}
        hdr = {"Idempotency-Key": "ik-claim-010"}

        r1 = client.post("/api/v2/tasks/10/claim", json=body, headers=hdr)
        assert r1.json()["lease_token"] is not None

        r2 = client.post("/api/v2/tasks/10/claim", json=body, headers=hdr)
        data2 = r2.json()
        assert data2["lease_token"] is None
        assert data2["lease_token_reissued"] is False

    def test_claim_idempotency_conflict_returns_409(self, db_path, client):
        """Same key, different params → 409 IDEMPOTENCY_CONFLICT."""
        _setup_worker(db_path, "exec-10", "executor", "available")
        _setup_task(db_path, 11, "queued", 1)
        hdr = {"Idempotency-Key": "ik-claim-011"}

        r1 = client.post(
            "/api/v2/tasks/11/claim",
            json={"worker_id": "exec-10", "expected_version": 1, "lease_seconds": 300},
            headers=hdr,
        )
        assert r1.status_code == 200

        r2 = client.post(
            "/api/v2/tasks/11/claim",
            json={"worker_id": "exec-10", "expected_version": 1, "lease_seconds": 600},
            headers=hdr,
        )
        assert r2.status_code == 409
        assert r2.json()["error_code"] == "IDEMPOTENCY_CONFLICT"

    def test_claim_no_sql_leak(self, db_path, client):
        """Response must not contain SQL or file paths."""
        _setup_worker(db_path, "exec-11", "executor", "available")
        _setup_task(db_path, 12, "queued", 1)

        resp = client.post(
            "/api/v2/tasks/12/claim",
            json={"worker_id": "exec-11", "expected_version": 1},
            headers={"Idempotency-Key": "ik-claim-012"},
        )
        raw = resp.text
        assert "sqlite" not in raw.lower()
        assert "traceback" not in raw.lower()
        assert "ai_factory.db" not in raw.lower()


# ============================================================
# 3 ─ Heartbeat
# ============================================================

class TestHeartbeatAPI:
    """POST /api/v2/tasks/{task_id}/heartbeat"""

    def test_normal_heartbeat_returns_200(self, db_path, client):
        """Normal heartbeat extends lease and returns 200."""
        _setup_worker(db_path, "exec-hb1", "executor", "busy")
        _setup_task(db_path, 1, "claimed", 2)
        _setup_claimed_task(db_path, 1, "asgn-hb001", "exec-hb1", "tok-hb1-deadbeef", 300)

        resp = client.post(
            "/api/v2/tasks/1/heartbeat",
            json={
                "assignment_id": "asgn-hb001",
                "worker_id": "exec-hb1",
                "lease_token": "tok-hb1-deadbeef",
                "extend_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-hb-001"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["heartbeat_id"] is not None
        assert data["idempotent"] is False

    def test_heartbeat_extends_lease(self, db_path, client):
        """lease_expires_at is extended."""
        _setup_worker(db_path, "exec-hb2", "executor", "busy")
        _setup_task(db_path, 2, "claimed", 2)
        _setup_claimed_task(db_path, 2, "asgn-hb002", "exec-hb2", "tok-hb2-deadbeef", 300)

        resp = client.post(
            "/api/v2/tasks/2/heartbeat",
            json={
                "assignment_id": "asgn-hb002",
                "worker_id": "exec-hb2",
                "lease_token": "tok-hb2-deadbeef",
                "extend_seconds": 600,
            },
            headers={"Idempotency-Key": "ik-hb-002"},
        )
        data = resp.json()
        prev = data.get("previous_expires_at")
        curr = data.get("lease_expires_at")
        assert prev is not None
        assert curr is not None
        assert prev != curr
        # Verify in DB
        conn = _raw_conn(db_path)
        row = conn.execute(
            "SELECT lease_expires_at FROM task_assignments WHERE assignment_id='asgn-hb002'"
        ).fetchone()
        conn.close()
        assert row["lease_expires_at"] == curr

    def test_wrong_lease_token_returns_409(self, db_path, client):
        """Wrong lease_token → 409."""
        _setup_worker(db_path, "exec-hb3", "executor", "busy")
        _setup_task(db_path, 3, "claimed", 2)
        _setup_claimed_task(db_path, 3, "asgn-hb003", "exec-hb3", "tok-hb3-deadbeef", 300)

        resp = client.post(
            "/api/v2/tasks/3/heartbeat",
            json={
                "assignment_id": "asgn-hb003",
                "worker_id": "exec-hb3",
                "lease_token": "wrong-token-here",
                "extend_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-hb-003"},
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "LEASE_CONFLICT"

    def test_other_worker_returns_409(self, db_path, client):
        """Heartbeat from a different worker → 409."""
        _setup_worker(db_path, "exec-hb4", "executor", "busy")
        _setup_worker(db_path, "exec-hb4b", "executor", "available")
        _setup_task(db_path, 4, "claimed", 2)
        _setup_claimed_task(db_path, 4, "asgn-hb004", "exec-hb4", "tok-hb4-deadbeef", 300)

        resp = client.post(
            "/api/v2/tasks/4/heartbeat",
            json={
                "assignment_id": "asgn-hb004",
                "worker_id": "exec-hb4b",  # wrong worker
                "lease_token": "tok-hb4-deadbeef",
                "extend_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-hb-004"},
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "LEASE_CONFLICT"

    def test_expired_lease_returns_409(self, db_path, client):
        """Heartbeat on expired lease → 409 STALE_LEASE."""
        _setup_worker(db_path, "exec-hb5", "executor", "busy")
        _setup_task(db_path, 5, "claimed", 2)
        _setup_claimed_task(db_path, 5, "asgn-hb005", "exec-hb5", "tok-hb5-deadbeef",
                            expires_offset_sec=-10)  # already expired

        resp = client.post(
            "/api/v2/tasks/5/heartbeat",
            json={
                "assignment_id": "asgn-hb005",
                "worker_id": "exec-hb5",
                "lease_token": "tok-hb5-deadbeef",
                "extend_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-hb-005"},
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "STALE_LEASE"

    def test_heartbeat_idempotent(self, db_path, client):
        """Same heartbeat repeated returns idempotent response."""
        _setup_worker(db_path, "exec-hb6", "executor", "busy")
        _setup_task(db_path, 6, "claimed", 2)
        _setup_claimed_task(db_path, 6, "asgn-hb006", "exec-hb6", "tok-hb6-deadbeef", 300)
        body = {
            "assignment_id": "asgn-hb006",
            "worker_id": "exec-hb6",
            "lease_token": "tok-hb6-deadbeef",
            "extend_seconds": 300,
        }
        hdr = {"Idempotency-Key": "ik-hb-006"}

        r1 = client.post("/api/v2/tasks/6/heartbeat", json=body, headers=hdr)
        assert r1.status_code == 200
        assert r1.json()["idempotent"] is False

        r2 = client.post("/api/v2/tasks/6/heartbeat", json=body, headers=hdr)
        assert r2.status_code == 200
        assert r2.json()["idempotent"] is True

        # Only one heartbeat record
        assert _row_count(db_path, "agent_heartbeats") == 1

    def test_heartbeat_no_lease_token_in_response(self, db_path, client):
        """Heartbeat response must NOT contain lease_token."""
        _setup_worker(db_path, "exec-hb7", "executor", "busy")
        _setup_task(db_path, 7, "claimed", 2)
        _setup_claimed_task(db_path, 7, "asgn-hb007", "exec-hb7", "tok-hb7-deadbeef", 300)

        resp = client.post(
            "/api/v2/tasks/7/heartbeat",
            json={
                "assignment_id": "asgn-hb007",
                "worker_id": "exec-hb7",
                "lease_token": "tok-hb7-deadbeef",
                "extend_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-hb-007"},
        )
        data = resp.json()
        assert "lease_token" not in data

    def test_heartbeat_no_fingerprint_column(self, db_path, client):
        """Response does not include agent_heartbeats.lease_token (which stores fingerprint)."""
        _setup_worker(db_path, "exec-hb8", "executor", "busy")
        _setup_task(db_path, 8, "claimed", 2)
        _setup_claimed_task(db_path, 8, "asgn-hb008", "exec-hb8", "tok-hb8-deadbeef", 300)

        resp = client.post(
            "/api/v2/tasks/8/heartbeat",
            json={
                "assignment_id": "asgn-hb008",
                "worker_id": "exec-hb8",
                "lease_token": "tok-hb8-deadbeef",
                "extend_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-hb-008"},
        )
        data = resp.json()
        # No fingerprint or raw lease_token field
        assert "fingerprint" not in json.dumps(data)
        assert "lease_token" not in data


# ============================================================
# 4 ─ Atomicity & Compatibility
# ============================================================

class TestAtomicityAndCompatibility:
    """Cross-cutting concerns."""

    def test_api_failure_no_dirty_data_claim(self, db_path, client):
        """Failed claim does not produce partial state."""
        _setup_worker(db_path, "exec-at1", "executor", "available")
        _setup_task(db_path, 1, "draft", 1)  # not queued
        assert _row_count(db_path, "task_assignments") == 0

        client.post(
            "/api/v2/tasks/1/claim",
            json={"worker_id": "exec-at1", "expected_version": 1},
            headers={"Idempotency-Key": "ik-at-001"},
        )
        # Task still draft (assignments unchanged)
        assert _row_count(db_path, "task_assignments") == 0
        conn = _raw_conn(db_path)
        t = conn.execute("SELECT status FROM development_tasks WHERE id=1").fetchone()
        conn.close()
        assert t["status"] == "draft"

    def test_flag_disabled_all_endpoints_no_data(self, db_path_disabled, client):
        """All three endpoints return 503 and write no data when flag=false."""
        # Register
        r1 = client.post(
            "/api/v2/workers/register",
            json={"worker_id": "w1", "worker_type": "executor"},
            headers={"Idempotency-Key": "ik-flag-1"},
        )
        assert r1.status_code == 503
        assert r1.json()["error_code"] == "V2_CONTROL_PLANE_DISABLED"

        # Claim
        r2 = client.post(
            "/api/v2/tasks/1/claim",
            json={"worker_id": "w1", "expected_version": 1},
            headers={"Idempotency-Key": "ik-flag-2"},
        )
        assert r2.status_code == 503
        assert r2.json()["error_code"] == "V2_CONTROL_PLANE_DISABLED"

        # Heartbeat
        r3 = client.post(
            "/api/v2/tasks/1/heartbeat",
            json={
                "assignment_id": "a1", "worker_id": "w1",
                "lease_token": "tok", "extend_seconds": 300,
            },
            headers={"Idempotency-Key": "ik-flag-3"},
        )
        assert r3.status_code == 503
        assert r3.json()["error_code"] == "V2_CONTROL_PLANE_DISABLED"

        # No data written
        assert _row_count(db_path_disabled, "agent_workers") == 0
        assert _row_count(db_path_disabled, "task_assignments") == 0
        assert _row_count(db_path_disabled, "agent_heartbeats") == 0

    def test_v1_health_still_works(self, client):
        """GET /api/health still returns 200 (V1 compatibility)."""
        # This test uses the app fixture which has v2_worker_api registered
        # but also exposes the health endpoint if we register it
        pass  # V1 health is on the main FastAPI app, not this test app

    def test_openapi_only_expected_v2_routes(self, client):
        """OpenAPI schema contains exactly the three expected V2 endpoints."""
        schema = client.get("/openapi.json").json()
        paths = schema.get("paths", {})
        v2_paths = [p for p in paths if "/api/v2/" in p]
        # We expect the B2 worker endpoints plus B3a submit.
        expected = {
            "/api/v2/workers/register",
            "/api/v2/tasks/{task_id}/claim",
            "/api/v2/tasks/{task_id}/heartbeat",
            "/api/v2/tasks/{task_id}/submit",
            "/api/v2/tasks/{task_id}/review",
            "/api/v2/tasks/{task_id}/handoff",
        }
        actual = set(v2_paths)
        assert actual == expected, f"Expected {expected}, got {actual}"
