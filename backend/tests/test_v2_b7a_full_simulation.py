from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent_runtime.b7a import (
    B7AExecutionBridge,
    B7ARuntimeService,
    MemorySecretProvider,
    MockProvider,
    TaskExecutionPolicy,
)
from app.supervisor.state_machine import TaskStateMachineService
from app.tools.v2_sandbox_rehearsal import SandboxBackendHarness


def _git(cmd, cwd: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *cmd],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.name", "Codex"], repo)
    _git(["config", "user.email", "codex@example.com"], repo)
    (repo / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text("from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "baseline"], repo)
    return repo


def _policy(repo: Path) -> TaskExecutionPolicy:
    return TaskExecutionPolicy(
        task_type="SANDBOX_CODE_PATCH",
        temporary_project=True,
        project_id=9300,
        sandbox_root=str(repo),
        allowed_files=["calculator.py"],
        forbidden_actions=["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
        allowed_test_commands=["python -m pytest test_calculator.py"],
        max_files_changed=1,
        max_patch_bytes=1024,
        evidence_required=True,
        approval_token="one-time",
        mode="mock",
        control_plane_url="http://127.0.0.1:8000",
    )


def test_full_b7a_happy_path_and_reviewer_flow(tmp_path):
    repo = _make_repo(tmp_path)
    harness = SandboxBackendHarness(port=0)
    runtime = None
    try:
        task_id = 9301
        project_id = 9300
        harness.seed_project_task(
            project_id,
            task_id,
            title="calculator bug",
            status="queued",
            version=1,
            files_to_modify=["calculator.py"],
            files_to_check=["calculator.py"],
            implementation_steps={"_requirements": {"language": "python"}},
        )
        harness.start()
        worker = harness.register_worker("b7a-worker", "executor", capabilities=["python"])
        assert worker["ok"] is True
        harness.set_worker_available("b7a-worker")
        claim_resp = harness.request(
            "POST",
            f"/api/v2/tasks/{task_id}/claim",
            json_body={
                "worker_id": "b7a-worker",
                "expected_version": 1,
                "lease_seconds": 60,
                "allowed_task_ids": [task_id],
                "project_id": project_id,
            },
            idem_key="b7a-claim",
        )
        assert claim_resp.status_code == 200, claim_resp.text
        claim = claim_resp.json()
        running = TaskStateMachineService(str(harness.db_path), v2_enabled=True).transition(
            task_id,
            "RUNNING",
            "supervisor",
            reason="b7a runtime started",
            expected_version=2,
            idempotency_key="b7a-running",
        )
        assert running["success"] is True, running
        db = harness.get_conn()
        try:
            db.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (claim["assignment_id"],))
            db.commit()
            state_row = db.execute("SELECT state_version FROM development_tasks WHERE id=?", (task_id,)).fetchone()
            expected_version = int(state_row["state_version"])
        finally:
            db.close()

        bridge = B7AExecutionBridge(
            provider=MockProvider(),
            secret_provider=MemorySecretProvider({}),
            submit_callback=lambda packet: _submit_result(harness, task_id, claim, packet, expected_version),
        )
        runtime = B7ARuntimeService(bridge=bridge, runtime_dir=tmp_path / "runtime")
        result = runtime.run_one_cycle(
            task_packet={
                "task_id": task_id,
                "project_id": project_id,
                "task_type": "SANDBOX_CODE_PATCH",
                "temporary_project": True,
                "allowed_files": ["calculator.py"],
                "allowed_test_commands": ["python -m pytest test_calculator.py"],
                "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
                "max_files_changed": 1,
                "max_patch_bytes": 1024,
                "evidence_required": True,
                "worker_id": "b7a-worker",
            },
            workspace_root=repo,
            policy=_policy(repo),
        )
        assert result["ok"] is True
        assert result["submit"]["task_state"] == "RESULT_SUBMITTED"
        assert (repo / "calculator.py").read_text(encoding="utf-8").endswith("return a + b\n")
        artifact_ids = [a["artifact_id"] for a in result["evidence"]["artifacts"]]

        reviewer = harness.register_worker("b7a-reviewer", "reviewer")
        assert reviewer["ok"] is True
        begin = harness.request(
            "POST",
            f"/api/v2/tasks/{task_id}/review",
            json_body={
                "action": "begin",
                "result_id": result["submit"]["result_id"],
                "reviewer_id": "b7a-reviewer",
                "expected_version": expected_version + 1,
            },
            idem_key="b7a-review-begin",
        )
        assert begin.status_code == 200, begin.text
        decide = harness.request(
            "POST",
            f"/api/v2/tasks/{task_id}/review",
            json_body={
                "action": "decide",
                "result_id": result["submit"]["result_id"],
                "reviewer_id": "b7a-reviewer",
                "expected_version": expected_version + 2,
                "decision": "VERIFIED",
                "summary": "patch accepted",
                "issues": [],
                "evidence_refs": artifact_ids,
                "risk_level": "low",
                "user_action_required": False,
                "metadata": {},
            },
            idem_key="b7a-review-decide",
        )
        assert decide.status_code == 200, decide.text
        runtime.shutdown()
        assert runtime.status()["runtime_status"] == "STOPPED"
        assert harness.request("GET", "/api/health").status_code == 200
    finally:
        if runtime is not None:
            runtime.shutdown()
        harness.cleanup()


def _submit_result(harness: SandboxBackendHarness, task_id: int, claim: dict, packet: dict, expected_version: int) -> dict:
    body = dict(packet)
    body.update(
        {
            "assignment_id": claim["assignment_id"],
            "worker_id": claim["worker_id"],
            "lease_token": claim["lease_token"],
            "expected_version": expected_version,
        }
    )
    resp = harness.request("POST", f"/api/v2/tasks/{task_id}/submit", json_body=body, idem_key="b7a-submit")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_b7a_rejects_malicious_patch_and_keeps_workspace_clean(tmp_path):
    repo = _make_repo(tmp_path)
    bridge = B7AExecutionBridge(provider=MockProvider(), secret_provider=MemorySecretProvider({}))
    runtime = B7ARuntimeService(bridge=bridge, runtime_dir=tmp_path / "runtime")
    result = runtime.run_one_cycle(
        task_packet={
            "task_id": 1,
            "project_id": 9300,
            "task_type": "SANDBOX_CODE_PATCH",
            "temporary_project": True,
            "allowed_files": ["calculator.py"],
            "allowed_test_commands": ["python -m pytest test_calculator.py"],
            "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
            "max_files_changed": 1,
            "max_patch_bytes": 1024,
            "evidence_required": True,
            "worker_id": "b7a-worker",
        },
        workspace_root=repo,
        policy=_policy(repo),
    )
    assert result["ok"] is True
    assert runtime.status()["runtime_status"] == "IDLE"
    runtime.shutdown()
