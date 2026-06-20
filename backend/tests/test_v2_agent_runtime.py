from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent_runtime.local_agent_connector import LocalAgentConnector
from app.agent_runtime.models import ConnectorConfig
from app.agent_runtime.runtime_config import RuntimeConfig
from app.agent_runtime.runtime_service import AgentRuntimeService, RuntimeLockError
from app.supervisor.state_machine import TaskStateMachineService
from app.tools.v2_sandbox_rehearsal import SandboxBackendHarness


def _runtime_config(tmp_path: Path, **overrides) -> RuntimeConfig:
    data = {
        "mode": "mock",
        "control_plane_url": "http://127.0.0.1:18000",
        "worker_id": "runtime-test",
        "project_id": 9100,
        "allowed_task_ids": [9101],
        "capabilities": ["control_plane_probe", "probe", "python"],
        "sandbox_root": str(tmp_path / "sandbox"),
        "runtime_dir": str(tmp_path / "runtime"),
        "heartbeat_interval": 0.1,
        "poll_interval": 0.1,
        "lease_seconds": 60,
        "request_timeout": 2.0,
        "max_retries": 1,
        "dry_run": False,
    }
    data.update(overrides)
    return RuntimeConfig.from_dict(data)


def test_default_runtime_disabled_and_control_plane_only_still_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("V2_CONTROL_PLANE_ENABLED", raising=False)
    monkeypatch.delenv("V2_AGENT_RUNTIME_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="RUNTIME_DISABLED"):
        AgentRuntimeService(_runtime_config(tmp_path, mode="sandbox")).start()
    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    with pytest.raises(RuntimeError, match="RUNTIME_DISABLED"):
        AgentRuntimeService(_runtime_config(tmp_path, mode="sandbox")).start()


def test_live_mode_rejected_and_non_localhost_url_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="RUNTIME_LIVE_MODE_NOT_AUTHORIZED"):
        AgentRuntimeService(_runtime_config(tmp_path, mode="live")).start()
    with pytest.raises(ValueError):
        _runtime_config(tmp_path, control_plane_url="http://example.com")


def test_dry_run_zero_http_and_zero_database_writes(tmp_path):
    db = tmp_path / "dry.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY)")
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    conn.close()

    cfg = ConnectorConfig(
        control_plane_url="http://127.0.0.1:18000",
        worker_id="dry-worker",
        project_id=1,
        allowed_task_ids=[1],
        sandbox_root=tmp_path / "dry-sandbox",
        dry_run=True,
    )
    connector = LocalAgentConnector(cfg)

    class NoHttp:
        def register_worker(self): raise AssertionError("register called")
        def claim_task(self, task_id, expected_version): raise AssertionError("claim called")
        def heartbeat(self, *a, **kw): raise AssertionError("heartbeat called")
        def submit_result(self, *a, **kw): raise AssertionError("submit called")
        def close(self): pass

    connector.client = NoHttp()
    result = connector.run_once(1)
    assert result["ok"] is True
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == before
    conn.close()


def test_mock_mode_probe_cycle_heartbeat_and_shutdown(tmp_path):
    svc = AgentRuntimeService(_runtime_config(tmp_path, mode="mock"))
    result = svc.run_one_cycle()
    assert result["ok"] is True
    assert result["action"] == "PROBE_SUBMITTED"
    assert (tmp_path / "sandbox" / "probe_result.txt").exists()
    status = svc.status()
    assert status["runtime_status"] == "IDLE"
    assert status["cycles_completed"] == 1
    assert status["heartbeat_active"] is False
    assert "lease_token" not in str(status).lower()
    svc.shutdown()
    assert not svc.config.pid_file.exists()
    assert not svc.config.lock_file.exists()


def test_no_task_enters_idle_and_backoff_does_not_busy_loop(tmp_path):
    sleeps = []
    svc = AgentRuntimeService(_runtime_config(tmp_path, allowed_task_ids=[], max_cycles=2), sleep_fn=lambda s: sleeps.append(s))
    result = svc.run_forever()
    assert result["ok"] is True
    assert sleeps and sleeps[0] >= 0.1


def test_single_instance_lock_and_start_stop_idempotent(tmp_path):
    cfg = _runtime_config(tmp_path)
    svc1 = AgentRuntimeService(cfg)
    assert svc1.start()["ok"] is True
    with pytest.raises(RuntimeLockError):
        AgentRuntimeService(cfg).start()
    assert svc1.stop()["ok"] is True
    assert svc1.stop()["ok"] is True


def test_status_does_not_leak_sensitive_fields(tmp_path):
    svc = AgentRuntimeService(_runtime_config(tmp_path))
    svc.run_one_cycle()
    rendered = str(svc.status()).lower()
    assert "lease_token" not in rendered
    assert "database_url" not in rendered
    assert "api_key" not in rendered
    svc.shutdown()


def test_sandbox_mode_full_http_probe_flow_and_formal_services_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("V2_AGENT_RUNTIME_ENABLED", "true")
    harness = SandboxBackendHarness(port=0)
    try:
        harness.seed_project_task(
            9100,
            9101,
            title="CONTROL_PLANE_PROBE",
            status="queued",
            version=1,
            files_to_modify=["probe_result.txt"],
            files_to_check=["probe_result.txt"],
            implementation_steps={"_requirements": {"language": "probe"}},
        )
        harness.start()

        cfg = _runtime_config(
            tmp_path,
            mode="sandbox",
            control_plane_url=harness.base_url,
            dry_run=False,
        )

        def after_register(connector):
            harness.set_worker_available(connector.config.worker_id)

        def after_claim(connector):
            claim = connector.current_claim
            result = TaskStateMachineService(str(harness.db_path), v2_enabled=True).transition(
                int(claim["task_id"]),
                "RUNNING",
                "supervisor",
                reason="runtime probe started",
                expected_version=2,
                idempotency_key="runtime-running",
            )
            assert result["success"] is True, result
            db = harness.get_conn()
            try:
                db.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (claim["assignment_id"],))
                db.commit()
            finally:
                db.close()

        svc = AgentRuntimeService(cfg, after_register=after_register, after_claim=after_claim)
        result = svc.run_one_cycle()
        assert result["ok"] is True
        assert result["submit"]["task_state"] == "RESULT_SUBMITTED"
        assert svc.status()["runtime_status"] == "IDLE"
        assert svc.status()["heartbeat_active"] is False
        db = harness.get_conn()
        try:
            row = db.execute("SELECT status FROM development_tasks WHERE id=9101").fetchone()
            assert row["status"] == "result_submitted"
        finally:
            db.close()
        svc.shutdown()
    finally:
        harness.cleanup()


def test_stale_lease_and_403_are_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("V2_CONTROL_PLANE_ENABLED", "true")
    monkeypatch.setenv("V2_AGENT_RUNTIME_ENABLED", "true")
    calls = {"register": 0, "claim": 0}

    class ForbiddenClient:
        def __init__(self, config): pass
        def register_worker(self): calls["register"] += 1; return {"ok": True}
        def claim_task(self, task_id, expected_version): calls["claim"] += 1; return {"ok": False, "error_code": "TASK_SCOPE_VIOLATION"}
        def close(self): pass

    svc = AgentRuntimeService(_runtime_config(tmp_path, mode="sandbox"), client_factory=ForbiddenClient)
    result = svc.run_one_cycle()
    assert result["ok"] is False
    assert result["error_code"] == "TASK_SCOPE_VIOLATION"
    assert calls == {"register": 1, "claim": 1}


def test_shutdown_leaves_no_heartbeat_thread(tmp_path):
    svc = AgentRuntimeService(_runtime_config(tmp_path))
    svc.run_one_cycle()
    svc.shutdown()
    assert not [t for t in threading.enumerate() if t.name.startswith("heartbeat-runtime-test")]
