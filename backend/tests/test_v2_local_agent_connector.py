"""Tests for the local sandbox agent connector MVP."""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent_runtime.client import ControlPlaneClient
from app.agent_runtime.local_agent_connector import LocalAgentConnector
from app.agent_runtime.models import ConnectorConfig
from app.agent_runtime.probe_executor import _resolve_inside_root, execute_probe_task
from app.supervisor.state_machine import TaskStateMachineService
from app.supervisor.worker_registry import WorkerRegistryService
from app.tools.v2_sandbox_rehearsal import SandboxBackendHarness


def _seed_task(harness: SandboxBackendHarness, task_id: int, project_id: int) -> None:
    harness.seed_project_task(
        project_id,
        task_id,
        title="probe-task",
        status="queued",
        version=1,
        files_to_modify=["probe_result.txt"],
        files_to_check=["probe_result.txt"],
        implementation_steps={"_requirements": {"language": "python"}},
    )


def _new_config(harness: SandboxBackendHarness, worker_id: str, *, dry_run: bool = True) -> ConnectorConfig:
    sandbox_root = Path(tempfile.mkdtemp(prefix=f"sandbox_{worker_id}_"))
    return ConnectorConfig(
        control_plane_url=harness.base_url,
        worker_id=worker_id,
        worker_type="executor",
        project_id=9100,
        allowed_task_ids=[9101],
        capabilities=["python"],
        sandbox_root=sandbox_root,
        heartbeat_interval=0.2,
        lease_seconds=60,
        request_timeout=5.0,
        max_retries=2,
        dry_run=dry_run,
    )


def _force_running(harness: SandboxBackendHarness, task_id: int, assignment_id: str) -> None:
    result = TaskStateMachineService(str(harness.db_path), v2_enabled=True).transition(
        task_id,
        "RUNNING",
        "supervisor",
        reason="worker started execution",
        expected_version=2,
        idempotency_key=f"state-{task_id}-running",
    )
    assert result["success"] is True, result
    conn = harness.get_conn()
    try:
        conn.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (assignment_id,))
        conn.commit()
    finally:
        conn.close()


def test_register_claim_heartbeat_probe_and_submit_integrates_against_live_http():
    harness = SandboxBackendHarness(port=0)
    try:
        task_id = 9101
        project_id = 9100
        _seed_task(harness, task_id, project_id)
        harness.start()
        connector = LocalAgentConnector(_new_config(harness, "agent-a", dry_run=False))

        reg = connector.register()
        assert reg["ok"] is True
        harness.set_worker_available("agent-a")

        claim = connector.claim_once(task_id)
        assert claim["ok"] is True
        assert claim["task_id"] == task_id

        _force_running(harness, task_id, claim["assignment_id"])
        hb = connector.start_heartbeat()
        assert hb is not None and hb.is_running() is True

        probe = connector.execute_probe_task()
        assert probe["ok"] is True
        assert Path(probe["file_path"]).exists()
        assert Path(probe["file_path"]).name == "probe_result.txt"

        submit = connector.submit_result(probe)
        assert submit["ok"] is True
        assert submit["task_state"] == "RESULT_SUBMITTED"

        reviewer = harness.register_worker("rev-a", "reviewer")
        assert reviewer["ok"] is True
        begin = harness.request(
            "POST",
            f"/api/v2/tasks/{task_id}/review",
            json_body={"action": "begin", "result_id": submit["result_id"], "reviewer_id": "rev-a", "expected_version": 4},
            idem_key="review-begin-a",
        )
        assert begin.status_code == 200, begin.text
        decision = harness.request(
            "POST",
            f"/api/v2/tasks/{task_id}/review",
            json_body={
                "action": "decide",
                "result_id": submit["result_id"],
                "reviewer_id": "rev-a",
                "expected_version": 5,
                "decision": "VERIFIED",
                "summary": "probe accepted",
                "issues": [],
                "evidence_refs": [probe["artifact"]["artifact_id"]],
                "risk_level": "low",
                "user_action_required": False,
                "metadata": {},
            },
            idem_key="review-decide-a",
        )
        assert decision.status_code == 200, decision.text

        connector.stop_heartbeat()
        connector.shutdown()
        conn = harness.get_conn()
        try:
            task = conn.execute("SELECT status FROM development_tasks WHERE id=?", (task_id,)).fetchone()
            worker = conn.execute("SELECT status FROM agent_workers WHERE worker_id='agent-a'").fetchone()
        finally:
            conn.close()
        assert task["status"] == "verified"
        assert worker["status"] in ("available", "registered")
    finally:
        harness.cleanup()


def test_connector_path_safety_and_probe_executor_rules():
    root = Path(tempfile.mkdtemp(prefix="probe_root_"))
    try:
        good = _resolve_inside_root(root, "probe_result.txt")
        assert good.parent == root.resolve(strict=False)
        with pytest.raises(ValueError):
            _resolve_inside_root(root, "../escape.txt")
        with pytest.raises(ValueError):
            _resolve_inside_root(root, r"C:\\Windows\\escape.txt")
        with pytest.raises(ValueError):
            _resolve_inside_root(root, r"\\\\server\\share\\escape.txt")
        with pytest.raises(ValueError):
            _resolve_inside_root(root, "file://escape.txt")
        with pytest.raises(ValueError):
            _resolve_inside_root(root, "http://example.com/escape.txt")

        symlink_dir = root / "link"
        outside = Path(tempfile.mkdtemp(prefix="probe_outside_"))
        try:
            try:
                symlink_dir.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("symlink creation is not supported in this environment")
            with pytest.raises(ValueError):
                _resolve_inside_root(root, "link/escape.txt")
        finally:
            for item in outside.iterdir():
                if item.is_file():
                    item.unlink()
            outside.rmdir()
    finally:
        for item in root.iterdir():
            try:
                if item.is_symlink() or item.is_file():
                    item.unlink()
                elif item.is_dir():
                    item.rmdir()
            except Exception:
                pass
        root.rmdir()


def test_retry_and_no_subprocess_rules(monkeypatch):
    class FakeResponse:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    config = ConnectorConfig(
        control_plane_url="http://127.0.0.1:18000",
        worker_id="agent-retry",
        worker_type="executor",
        project_id=9100,
        allowed_task_ids=[9101],
        capabilities=["python"],
        sandbox_root=Path(tempfile.mkdtemp(prefix="connector_retry_")),
        heartbeat_interval=0.1,
        lease_seconds=60,
        request_timeout=1.0,
        max_retries=2,
        dry_run=True,
    )
    client = ControlPlaneClient(config)
    calls = {"count": 0}

    def flaky_once(method, path, *, json_body, headers):
        calls["count"] += 1
        if calls["count"] < 2:
            raise requests.Timeout("timeout")
        return FakeResponse(200, {"ok": True})

    monkeypatch.setattr(client, "_request_once", flaky_once)
    body = client._request_json("POST", "/api/test", json_body={}, idem_key="retry-key")
    assert body["ok"] is True
    assert calls["count"] == 2

    connector = LocalAgentConnector(config)
    connector.current_claim = {
        "task_id": 9101,
        "assignment_id": "asgn-1",
        "worker_id": "agent-retry",
        "lease_token": "token",
        "state_version": 2,
    }
    probe = connector.execute_probe_task()
    assert probe["ok"] is True
    source = inspect.getsource(execute_probe_task)
    assert "subprocess" not in source.lower()
    connector.shutdown()


def test_local_host_only_urls_and_registration_flow():
    with pytest.raises(ValueError):
        ConnectorConfig(
            control_plane_url="https://example.com",
            worker_id="x",
            sandbox_root=Path(tempfile.mkdtemp(prefix="bad_url_")),
        )

    with pytest.raises(ValueError):
        ConnectorConfig(
            control_plane_url="http://example.com",
            worker_id="x",
            sandbox_root=Path(tempfile.mkdtemp(prefix="bad_url_")),
        )

    harness = SandboxBackendHarness(port=0)
    try:
        _seed_task(harness, 9101, 9100)
        harness.start()
        connector = LocalAgentConnector(_new_config(harness, "agent-b", dry_run=True))
        reg = connector.register()
        assert reg["ok"] is True
        assert reg["planned_action"] == "REGISTER_WORKER"
        claim = connector.claim_once(9101)
        assert claim["ok"] is True
        assert claim["planned_action"] == "CLAIM_TASK"
        conn = harness.get_conn()
        try:
            worker = conn.execute("SELECT worker_id FROM agent_workers WHERE worker_id='agent-b'").fetchone()
            assignments = conn.execute("SELECT COUNT(*) AS c FROM task_assignments WHERE worker_id='agent-b'").fetchone()
        finally:
            conn.close()
        assert worker is None
        assert assignments["c"] == 0
        connector.shutdown()
    finally:
        harness.cleanup()
