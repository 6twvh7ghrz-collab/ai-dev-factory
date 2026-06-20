"""V2.0-B5b: control plane end-to-end rehearsal.

These tests use the e2e backend fixture from conftest.py, which runs the app
against a temporary SQLite database copied from the production seed. The tests
exercise the public V2 HTTP API plus the formal supervisor services.
"""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import sys
import uuid
import importlib

import pytest
import requests

sys_path = os.path.join(os.path.dirname(__file__), "..")
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from app.supervisor.lease_recovery_service import LeaseRecoveryService
from app.supervisor.orchestration_service import SupervisorOrchestrationService
from app.supervisor.state_machine import TaskStateMachineService
from app.supervisor.task_handoff_service import TaskHandoffService
from app.supervisor.task_review_service import TaskReviewService
from app.supervisor.task_claim_service import TaskClaimService
from app.supervisor.worker_registry import WorkerRegistryService


pytestmark = pytest.mark.e2e


def _base_url() -> str:
    return os.environ["E2E_BASE_URL"].rstrip("/")


def _db_path() -> Path:
    return Path(os.environ["AI_FACTORY_DB_PATH"])


def _artifact_ref(task_id: int, key: str) -> str:
    return f"art-report-{task_id}-{key}"


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _conn():
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_MIGRATED = False


def _ensure_migrations():
    global _MIGRATED
    if _MIGRATED:
        return
    for mod_name in (
        "app.migrations.015_execution_artifacts",
        "app.migrations.016_v2_review_decisions",
        "app.migrations.017_v2_task_handoffs",
        "app.migrations.018_v2_supervisor_cycles",
    ):
        mod = importlib.import_module(mod_name)
        mod.upgrade(str(_db_path()))
    _MIGRATED = True


def _table_exists(name: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        return row is not None
    finally:
        conn.close()


def _post(path: str, json_body: dict, key: str):
    resp = _session().post(
        _base_url() + path,
        json=json_body,
        headers={"Idempotency-Key": key},
        timeout=30,
    )
    return resp


def _seed_task(project_id: int, task_id: int, title: str, *, requirements=None, status="queued", version=1):
    conn = _conn()
    try:
        conn.execute("INSERT OR IGNORE INTO projects (id, name, status) VALUES (?, ?, 'active')", (project_id, f"project-{project_id}"))
        conn.execute(
            """
            INSERT INTO development_tasks
            (id, project_id, title, description, task_type, status, state_version, last_state_change,
             dependencies, files_to_check, files_to_modify, test_steps, acceptance_criteria, implementation_steps)
            VALUES (?, ?, ?, ?, 'backend', ?, ?, datetime('now'), '[]', '[]', '[]', '[]', '[]', ?)
            """,
            (
                task_id,
                project_id,
                title,
                f"{title} description",
                status,
                version,
                json.dumps({"_requirements": requirements or {}}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _reset_case(project_id: int, task_ids: list[int], worker_ids: list[str]):
    _ensure_migrations()
    conn = _conn()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in ("task_events", "task_handoffs", "review_decisions", "execution_artifacts", "task_results", "task_assignments", "development_tasks", "supervisor_cycles"):
            if _table_exists(table):
                conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        if task_ids:
            q = ",".join("?" for _ in task_ids)
            conn.execute(f"DELETE FROM task_events WHERE task_id IN ({q})", task_ids)
            if _table_exists("task_handoffs"):
                conn.execute(f"DELETE FROM task_handoffs WHERE task_id IN ({q})", task_ids)
            if _table_exists("review_decisions"):
                conn.execute(f"DELETE FROM review_decisions WHERE task_id IN ({q})", task_ids)
            if _table_exists("execution_artifacts"):
                conn.execute(f"DELETE FROM execution_artifacts WHERE task_id IN ({q})", task_ids)
            if _table_exists("task_results"):
                conn.execute(f"DELETE FROM task_results WHERE task_id IN ({q})", task_ids)
            if _table_exists("task_assignments"):
                conn.execute(f"DELETE FROM task_assignments WHERE task_id IN ({q})", task_ids)
            conn.execute(f"DELETE FROM development_tasks WHERE id IN ({q})", task_ids)
        if worker_ids:
            q = ",".join("?" for _ in worker_ids)
            if _table_exists("task_assignments"):
                conn.execute(f"DELETE FROM task_assignments WHERE worker_id IN ({q})", worker_ids)
            if _table_exists("task_results"):
                conn.execute(f"DELETE FROM task_results WHERE worker_id IN ({q})", worker_ids)
            if _table_exists("task_handoffs"):
                conn.execute(f"DELETE FROM task_handoffs WHERE from_worker_id IN ({q}) OR to_worker_id IN ({q})", worker_ids * 2)
            if _table_exists("review_decisions"):
                conn.execute(f"DELETE FROM review_decisions WHERE reviewer_id IN ({q})", worker_ids)
            if _table_exists("task_events"):
                conn.execute(f"DELETE FROM task_events WHERE operator_id IN ({q})", worker_ids)
            if _table_exists("agent_capabilities"):
                conn.execute(f"DELETE FROM agent_capabilities WHERE worker_id IN ({q})", worker_ids)
            try:
                conn.execute(f"DELETE FROM worker_project_scopes WHERE worker_id IN ({q})", worker_ids)
            except sqlite3.OperationalError:
                pass
            conn.execute(f"DELETE FROM agent_workers WHERE worker_id IN ({q})", worker_ids)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()
    finally:
        conn.close()


def _seed_workers():
    conn = _conn()
    try:
        conn.execute("DELETE FROM agent_capabilities")
        conn.execute("DELETE FROM agent_workers WHERE worker_id IN ('exec-a','exec-b','rev-a')")
        conn.execute("INSERT INTO agent_workers (worker_id, worker_type, provider, display_name, status, max_concurrency, current_load, sandbox_profile_id, registered_at, last_seen_at, metadata_json, version) VALUES ('exec-b','executor','','Executor B','registered',1,0,'',datetime('now'),datetime('now'),'{}',1)")
        conn.execute("INSERT INTO agent_workers (worker_id, worker_type, provider, display_name, status, max_concurrency, current_load, sandbox_profile_id, registered_at, last_seen_at, metadata_json, version) VALUES ('rev-a','reviewer','','Reviewer A','available',1,0,'',datetime('now'),datetime('now'),'{}',1)")
        conn.commit()
    finally:
        conn.close()


def _set_worker_status(worker_id: str, status: str):
    result = WorkerRegistryService(str(_db_path()), v2_enabled=True).set_worker_status(worker_id, status)
    assert result["success"] is True, result


def _register_via_api(worker_id: str, worker_type: str, capabilities=None):
    body = {
        "worker_id": worker_id,
        "worker_type": worker_type,
        "provider": "local",
        "display_name": worker_id,
        "capabilities": capabilities or [],
        "sandbox_profile_id": "default",
        "metadata": {},
    }
    resp = _session().post(
        _base_url() + "/api/v2/workers/register",
        json=body,
        headers={"Idempotency-Key": f"register-{worker_id}"},
        timeout=30,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _claim(task_id: int, worker_id: str, expected_version: int, key: str, *, project_id: int):
    resp = _post(
        f"/api/v2/tasks/{task_id}/claim",
        {"worker_id": worker_id, "expected_version": expected_version, "lease_seconds": 300, "allowed_task_ids": [task_id], "project_id": project_id},
        key,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _heartbeat(task_id: int, assignment_id: str, worker_id: str, lease_token: str, key: str):
    resp = _post(
        f"/api/v2/tasks/{task_id}/heartbeat",
        {"assignment_id": assignment_id, "worker_id": worker_id, "lease_token": lease_token, "extend_seconds": 300},
        key,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _submit(task_id: int, assignment_id: str, worker_id: str, lease_token: str, expected_version: int, key: str, *, tests_failed=0, evidence_refs=None, files_changed=None):
    artifact_id = _artifact_ref(task_id, key)
    body = {
        "assignment_id": assignment_id,
        "worker_id": worker_id,
        "lease_token": lease_token,
        "expected_version": expected_version,
        "execution_id": assignment_id,
        "result_status": "submitted",
        "files_modified": files_changed or ["src/app.py"],
        "files_checked": ["tests/test_app.py"],
        "diff_summary": "implementation done",
        "tests": {"total": 4, "passed": 4 - tests_failed, "failed": tests_failed, "skipped": 0, "failed_count": tests_failed},
        "git_commit": "a" * 40,
        "git_branch": "feature/test",
        "base_commit": "b" * 40,
        "exit_code": 0 if tests_failed == 0 else 1,
        "stdout": "",
        "stderr": "",
        "manual_actions": [{"action": "none"}],
        "errors": [],
        "evidence_refs": evidence_refs or [artifact_id],
        "artifacts": [
            {"artifact_id": artifact_id, "artifact_type": "test_report", "uri": f"artifacts/{artifact_id}.txt", "size_bytes": 12, "mime_type": "text/plain", "metadata": {"kind": "report"}}
        ],
        "handoff_requested": False,
        "remaining_steps": [],
        "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": 50,
        "model_calls": 0,
        "repair_attempts": 0,
    }
    resp = _post(f"/api/v2/tasks/{task_id}/submit", body, key)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    data["artifact_id"] = artifact_id
    return data


def _begin_review(task_id: int, result_id: str, reviewer_id: str, version: int, key: str):
    resp = _post(
        f"/api/v2/tasks/{task_id}/review",
        {"action": "begin", "result_id": result_id, "reviewer_id": reviewer_id, "expected_version": version},
        key,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _decide(task_id: int, result_id: str, reviewer_id: str, version: int, decision: str, summary: str, issues, evidence_refs, key: str):
    resp = _post(
        f"/api/v2/tasks/{task_id}/review",
        {
            "action": "decide",
            "result_id": result_id,
            "reviewer_id": reviewer_id,
            "expected_version": version,
            "decision": decision,
            "summary": summary,
            "issues": issues,
            "evidence_refs": evidence_refs,
            "risk_level": "low",
            "user_action_required": False,
            "metadata": {},
        },
        key,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _handoff_request(task_id: int, assignment_id: str, from_worker_id: str, lease_token: str, reason_code: str, reason: str, *, key: str, completed_steps=None, remaining_steps=None, evidence_refs=None, forbidden_actions=None, tests_run=None, current_stage="implementation", git_head="abc123"):
    body = {
        "action": "request",
        "assignment_id": assignment_id,
        "from_worker_id": from_worker_id,
        "lease_token": lease_token,
        "reason_code": reason_code,
        "reason": reason,
        "completed_steps": completed_steps or ["registered", "claimed"],
        "remaining_steps": remaining_steps or ["review"],
        "recent_errors": [],
        "evidence_refs": evidence_refs or ["art-report"],
        "forbidden_actions": forbidden_actions or ["fake_result"],
        "files_changed": ["src/app.py"],
        "tests_run": tests_run or [{"name": "pytest", "status": "passed"}],
        "context_snapshot": {"current_stage": current_stage},
        "git_head": git_head,
        "current_stage": current_stage,
        "expires_seconds": 600,
    }
    resp = _post(f"/api/v2/tasks/{task_id}/handoff", body, key)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _handoff_accept(task_id: int, handoff_id: str, to_worker_id: str, version: int, key: str):
    body = {
        "action": "accept",
        "handoff_id": handoff_id,
        "to_worker_id": to_worker_id,
        "expected_version": version,
        "lease_seconds": 300,
    }
    resp = _post(f"/api/v2/tasks/{task_id}/handoff", body, key)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _patch_running(task_id: int, assignment_id: str, *, state_version: int):
    conn = _conn()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE task_assignments SET status='running', started_at=?, updated_at=? WHERE assignment_id=?", (now, now, assignment_id))
        conn.commit()
    finally:
        conn.close()


def test_happy_path_e2e():
    project_id = 9100
    task_id = 9101
    _reset_case(project_id, [task_id], ["exec-a", "exec-b", "rev-a"])
    _seed_task(project_id, task_id, "happy-path", requirements={"lang": "python"})
    _seed_workers()
    _register_via_api("exec-a", "executor", ["python"])
    _set_worker_status("exec-a", "available")
    _register_via_api("rev-a", "reviewer", [])

    plan = SupervisorOrchestrationService(str(_db_path()), v2_enabled=True).run_one_cycle(project_id, "cycle-happy-dry", dry_run=True)
    assert plan["planned_action"] == "CLAIM_TASK"
    assert plan["dry_run"] is True

    live = SupervisorOrchestrationService(str(_db_path()), v2_enabled=True).run_one_cycle(project_id, "cycle-happy-live", dry_run=False)
    assert live["planned_action"] == "CLAIM_TASK"
    assert live["success"] is True

    conn = _conn()
    try:
        task = conn.execute("SELECT status, state_version FROM development_tasks WHERE id=?", (task_id,)).fetchone()
        assignment = conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
        worker = conn.execute("SELECT status FROM agent_workers WHERE worker_id='exec-a'").fetchone()
        assert task["status"] == "claimed"
        assert task["state_version"] == 2
        assert assignment["status"] == "assigned"
        assert worker["status"] == "busy"
    finally:
        conn.close()

    state_machine = TaskStateMachineService(str(_db_path()), v2_enabled=True)
    running = state_machine.transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-run")
    assert running["success"] is True
    conn = _conn()
    try:
        claim_row = conn.execute("SELECT assignment_id, lease_token FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    _patch_running(task_id, claim_row["assignment_id"], state_version=3)
    _heartbeat(task_id, claim_row["assignment_id"], "exec-a", claim_row["lease_token"], "hb-1")

    submit = _submit(task_id, claim_row["assignment_id"], "exec-a", claim_row["lease_token"], 3, "submit-1")
    assert submit["task_state"] == "RESULT_SUBMITTED"
    assert submit["assignment_status"] == "completed"
    assert submit["worker_status"] == "available"
    assert submit["artifact_count"] == 1

    begin = _begin_review(task_id, submit["result_id"], "rev-a", 4, "review-begin")
    assert begin["task_state"] == "REVIEWING"

    decide = _decide(task_id, submit["result_id"], "rev-a", 5, "VERIFIED", "looks good", [], [submit["artifact_id"]], "review-decide")
    assert decide["task_state"] == "VERIFIED"

    final_plan = SupervisorOrchestrationService(str(_db_path()), v2_enabled=True).run_one_cycle(project_id, "cycle-happy-final", dry_run=True)
    assert final_plan["planned_action"] == "NO_ACTION"
    assert final_plan["task_id"] == task_id


def test_rework_handoff_and_reresult_flow():
    project_id = 9100
    task_id = 9102
    _reset_case(project_id, [task_id], ["exec-a", "exec-b", "rev-a"])
    _seed_task(project_id, task_id, "rework-path")
    _seed_workers()
    _register_via_api("exec-a", "executor", [])
    _register_via_api("rev-a", "reviewer", [])
    _set_worker_status("exec-a", "available")
    claim = _claim(task_id, "exec-a", 1, "claim-rework", project_id=project_id)
    TaskStateMachineService(str(_db_path()), v2_enabled=True).transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-rework")
    _patch_running(task_id, claim["assignment_id"], state_version=3)
    submit = _submit(task_id, claim["assignment_id"], "exec-a", claim["lease_token"], 3, "submit-rework")
    _begin_review(task_id, submit["result_id"], "rev-a", 4, "begin-rework")
    review = _decide(
        task_id,
        submit["result_id"],
        "rev-a",
        5,
        "REWORK",
        "needs adjustments",
        [{"severity": "high", "reason": "missing tests", "acceptance": "add tests", "suggested_fix": "expand coverage"}],
        [submit["artifact_id"]],
        "decide-rework",
    )
    assert review["task_state"] == "REWORK"

    _set_worker_status("exec-a", "registered")
    _set_worker_status("exec-b", "available")
    handoff = _handoff_request(
        task_id,
        claim["assignment_id"],
        "exec-a",
        claim["lease_token"],
        "REWORK_REQUIRED",
        "reviewer requested rework",
        key="handoff-rework",
        completed_steps=["implemented", "submitted"],
        remaining_steps=["fix tests", "resubmit"],
    )
    assert handoff["status"] == "pending"

    accepted = _handoff_accept(task_id, handoff["handoff_id"], "exec-b", 6, "handoff-accept")
    assert accepted["status"] == "accepted"
    assert accepted["assignment_id"]
    conn = _conn()
    try:
        accepted_row = conn.execute("SELECT lease_token FROM task_assignments WHERE assignment_id=?", (accepted["assignment_id"],)).fetchone()
    finally:
        conn.close()
    assert accepted_row is not None

    conn = _conn()
    try:
        old_assignment = conn.execute("SELECT status FROM task_assignments WHERE assignment_id=?", (claim["assignment_id"],)).fetchone()
        new_assignment = conn.execute("SELECT status, worker_id FROM task_assignments WHERE assignment_id=?", (accepted["assignment_id"],)).fetchone()
        old_worker = conn.execute("SELECT status FROM agent_workers WHERE worker_id='exec-a'").fetchone()
        new_worker = conn.execute("SELECT status FROM agent_workers WHERE worker_id='exec-b'").fetchone()
        assert old_assignment["status"] == "cancelled"
        assert new_assignment["status"] == "running"
        assert new_assignment["worker_id"] == "exec-b"
        assert old_worker["status"] == "registered"
        assert new_worker["status"] == "busy"
    finally:
        conn.close()

    submit2 = _submit(task_id, accepted["assignment_id"], "exec-b", accepted_row["lease_token"], 7, "submit-after-handoff")
    _begin_review(task_id, submit2["result_id"], "rev-a", 8, "begin-after-handoff")
    final = _decide(task_id, submit2["result_id"], "rev-a", 9, "VERIFIED", "fixed", [], [submit2["artifact_id"]], "decide-after-handoff")
    assert final["task_state"] == "VERIFIED"


def test_quota_exhausted_handoff_and_lease_expiry_paths():
    project_id = 9101
    task_id = 9103
    _reset_case(project_id, [task_id, 9104, 9105], ["exec-a", "exec-b", "rev-a"])
    _seed_task(project_id, task_id, "quota-and-expiry")
    _seed_workers()
    _register_via_api("exec-a", "executor", [])
    _set_worker_status("exec-a", "available")
    claim = _claim(task_id, "exec-a", 1, "claim-quota", project_id=project_id)
    TaskStateMachineService(str(_db_path()), v2_enabled=True).transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-quota")
    _patch_running(task_id, claim["assignment_id"], state_version=3)
    quota = _handoff_request(
        task_id,
        claim["assignment_id"],
        "exec-a",
        claim["lease_token"],
        "QUOTA_EXHAUSTED",
        "worker exhausted quota",
        key="handoff-quota",
        completed_steps=["step1"],
        remaining_steps=["step2"],
        evidence_refs=[_artifact_ref(task_id, "submit-quota")],
    )
    assert quota["status"] == "pending"
    assert quota["from_worker_id"] == "exec-a"
    _set_worker_status("exec-a", "registered")
    _set_worker_status("exec-b", "available")
    accepted = _handoff_accept(task_id, quota["handoff_id"], "exec-b", 3, "quota-accept")
    assert accepted["status"] == "accepted"
    _set_worker_status("exec-b", "registered")

    expiry_task = 9104
    _seed_task(project_id, expiry_task, "lease-expiry")
    _set_worker_status("exec-a", "available")
    claim2 = _claim(expiry_task, "exec-a", 1, "claim-expiry", project_id=project_id)
    conn = _conn()
    try:
        conn.execute("UPDATE task_assignments SET lease_expires_at=? WHERE assignment_id=?", ((datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), claim2["assignment_id"]))
        conn.commit()
    finally:
        conn.close()
    recovery = LeaseRecoveryService(str(_db_path()), v2_enabled=True).recover_assignment(claim2["assignment_id"], "expired lease", "recover-expiry")
    assert recovery["success"] is True, recovery
    conn = _conn()
    try:
        task_after = conn.execute("SELECT status FROM development_tasks WHERE id=?", (expiry_task,)).fetchone()
    finally:
        conn.close()
    assert task_after["status"] == "queued"

    running_task = 9105
    _seed_task(project_id, running_task, "running-expiry")
    claim3 = _claim(running_task, "exec-a", 1, "claim-running-expiry", project_id=project_id)
    TaskStateMachineService(str(_db_path()), v2_enabled=True).transition(running_task, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-running-expiry")
    _patch_running(running_task, claim3["assignment_id"], state_version=3)
    conn = _conn()
    try:
        conn.execute("UPDATE task_assignments SET lease_expires_at=? WHERE assignment_id=?", ((datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), claim3["assignment_id"]))
        conn.commit()
    finally:
        conn.close()
    recovery2 = LeaseRecoveryService(str(_db_path()), v2_enabled=True).recover_assignment(claim3["assignment_id"], "lease expired during execution", "recover-running")
    assert recovery2["task_state"] == "BLOCKED"


def test_need_user_and_blocked_stop_supervisor():
    project_id = 9102
    task_id = 9106
    _reset_case(project_id, [task_id, 9107], ["exec-a", "exec-b", "rev-a"])
    _seed_task(project_id, task_id, "need-user")
    _seed_workers()
    _register_via_api("exec-a", "executor", [])
    _set_worker_status("exec-a", "available")
    _register_via_api("rev-a", "reviewer", [])
    claim = _claim(task_id, "exec-a", 1, "claim-user", project_id=project_id)
    TaskStateMachineService(str(_db_path()), v2_enabled=True).transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-user")
    _patch_running(task_id, claim["assignment_id"], state_version=3)
    submit = _submit(task_id, claim["assignment_id"], "exec-a", claim["lease_token"], 3, "submit-user")
    _begin_review(task_id, submit["result_id"], "rev-a", 4, "begin-user")
    issue = [{"question": "Which option should we take?", "options": ["A", "B"], "risk": "medium"}]
    need_user = _decide(task_id, submit["result_id"], "rev-a", 5, "NEED_USER", "needs user input", issue, [submit["artifact_id"]], "decide-user")
    assert need_user["task_state"] == "NEED_USER"
    plan = SupervisorOrchestrationService(str(_db_path()), v2_enabled=True).run_one_cycle(project_id, "cycle-user", dry_run=True)
    assert plan["planned_action"] == "STOP_AND_WAIT_USER"

    blocked_project = 9104
    blocked_task = 9110
    _seed_task(blocked_project, blocked_task, "blocked", status="blocked")
    plan2 = SupervisorOrchestrationService(str(_db_path()), v2_enabled=True).run_one_cycle(blocked_project, "cycle-blocked", dry_run=True)
    assert plan2["planned_action"] == "STOP_AND_REPORT_BLOCKER"


def test_concurrency_and_idempotency_smoke():
    project_id = 9103
    task_id = 9108
    _reset_case(project_id, [task_id], ["exec-a", "exec-b", "rev-a"])
    _seed_task(project_id, task_id, "concurrency")
    _seed_workers()
    _register_via_api("exec-a", "executor", [])
    _set_worker_status("exec-a", "available")
    _register_via_api("rev-a", "reviewer", [])

    def claim_exec(worker_id, key):
        return TaskClaimService(str(_db_path()), v2_enabled=True).claim_task(
            task_id=task_id,
            worker_id=worker_id,
            expected_version=1,
            idempotency_key=key,
            allowed_task_ids=[task_id],
            project_id=project_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(claim_exec, "exec-a", "claim-c1"),
            pool.submit(claim_exec, "exec-b", "claim-c2"),
        ]
        results = [f.result() for f in futures]
    assert sum(1 for item in results if item.get("success")) == 1

    conn = _conn()
    try:
        assignment = conn.execute("SELECT assignment_id, worker_id, lease_token FROM task_assignments WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    finally:
        conn.close()
    TaskStateMachineService(str(_db_path()), v2_enabled=True).transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-idem")
    _patch_running(task_id, assignment["assignment_id"], state_version=3)
    submit = _submit(task_id, assignment["assignment_id"], assignment["worker_id"], assignment["lease_token"], 3, "submit-idem")
    submit2 = _submit(task_id, assignment["assignment_id"], assignment["worker_id"], assignment["lease_token"], 3, "submit-idem")
    assert submit2["idempotent"] is True
