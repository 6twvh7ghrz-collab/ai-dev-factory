"""V2 sandbox rehearsal runner.

This module provides a reusable temporary-backend harness for controlled V2
rehearsals. It never touches the user's live 8000/5173 services.
"""

from __future__ import annotations

import importlib
import gc
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import requests

from app.supervisor.lease_recovery_service import LeaseRecoveryService
from app.supervisor.orchestration_service import SupervisorOrchestrationService
from app.supervisor.state_machine import TaskStateMachineService
from app.supervisor.task_handoff_service import TaskHandoffService
from app.supervisor.task_review_service import TaskReviewService
from app.supervisor.worker_registry import WorkerRegistryService


BACKEND_DIR = Path(__file__).resolve().parents[2]
PROD_DB = BACKEND_DIR / "data" / "ai_factory.db"
MIGRATION_MODULES = (
    "app.migrations.012_v2_control_plane",
    "app.migrations.013_v2_worker_registry",
    "app.migrations.014_task_assignment_timeout_index",
    "app.migrations.015_execution_artifacts",
    "app.migrations.016_v2_review_decisions",
    "app.migrations.017_v2_task_handoffs",
    "app.migrations.018_v2_supervisor_cycles",
)


def _clear_proxy_env(env: dict[str, str]) -> dict[str, str]:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        env.pop(key, None)
    no_proxy = "localhost,127.0.0.1,::1"
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    return env


def _taskkill_exe() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "taskkill.exe"
    if candidate.exists():
        return str(candidate)
    return "taskkill"


def _free_port(preferred: int = 18000) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if preferred <= 0:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


def ensure_v2_migrations(db_path: Path) -> None:
    for module_name in MIGRATION_MODULES:
        mod = importlib.import_module(module_name)
        if hasattr(mod, "upgrade"):
            mod.upgrade(str(db_path))
        elif hasattr(mod, "migrate"):
            mod.migrate(str(db_path))
        else:
            raise AttributeError(f"{module_name} does not expose migrate/upgrade")


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class SandboxBackendHarness:
    """Owns a temporary SQLite database and a temporary uvicorn process."""

    port: int = 18000
    workspace_root: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="v2_sandbox_")))
    db_path: Path = field(init=False)
    process: Optional[subprocess.Popen] = field(default=None, init=False)
    api_request_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.port = _free_port(self.port)
        self.db_path = self.workspace_root / "sandbox.db"
        shutil.copy2(PROD_DB, self.db_path)
        ensure_v2_migrations(self.db_path)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        return session

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return

        last_error: Optional[Exception] = None
        for _ in range(3):
            env = _clear_proxy_env(os.environ.copy())
            env["DATABASE_URL"] = f"sqlite:///{self.db_path.as_posix()}"
            env["AI_FACTORY_DB_PATH"] = str(self.db_path)
            env["V2_CONTROL_PLANE_ENABLED"] = "true"
            env["PYTHONUNBUFFERED"] = "1"
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--log-level",
                    "warning",
                ],
                cwd=str(BACKEND_DIR),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                self.wait_ready()
                return
            except RuntimeError as exc:
                last_error = exc
                stderr = ""
                try:
                    stderr = self.process.stderr.read(2000).decode("utf-8", errors="replace") if self.process and self.process.stderr else ""
                except Exception:
                    pass
                self.stop()
                if "10048" not in stderr and "bind" not in stderr.lower() and "address already in use" not in stderr.lower():
                    raise
                self.port = _free_port(0)
        if last_error:
            raise last_error

    def wait_ready(self, timeout: int = 45) -> None:
        deadline = time.time() + timeout
        session = self._session()
        last_error: Optional[Exception] = None
        url = f"{self.base_url}/api/health"
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                stderr = ""
                try:
                    stderr = self.process.stderr.read(2000).decode("utf-8", errors="replace") if self.process.stderr else ""
                except Exception:
                    pass
                raise RuntimeError(f"temporary backend exited early: {stderr}")
            try:
                resp = session.get(url, timeout=3)
                self.api_request_count += 1
                if resp.status_code == 200:
                    return
            except Exception as exc:  # pragma: no cover - transient startup race
                last_error = exc
            time.sleep(1)
        stderr = ""
        try:
            stderr = self.process.stderr.read(2000).decode("utf-8", errors="replace") if self.process and self.process.stderr else ""
        except Exception:
            pass
        raise RuntimeError(f"temporary backend did not become healthy: {last_error}; stderr={stderr}")

    def request(self, method: str, path: str, *, json_body: Optional[dict] = None, idem_key: str = "sandbox-idem", timeout: int = 30):
        session = self._session()
        headers = {"Idempotency-Key": idem_key}
        resp = session.request(
            method,
            self.base_url.rstrip("/") + path,
            json=json_body,
            headers=headers,
            timeout=timeout,
        )
        self.api_request_count += 1
        return resp

    def register_worker(self, worker_id: str, worker_type: str, *, capabilities: Optional[list[str]] = None, display_name: str = "") -> dict:
        resp = self.request(
            "POST",
            "/api/v2/workers/register",
            json_body={
                "worker_id": worker_id,
                "worker_type": worker_type,
                "provider": "local",
                "display_name": display_name or worker_id,
                "capabilities": capabilities or [],
                "sandbox_profile_id": "sandbox-default",
                "metadata": {},
            },
            idem_key=f"register-{worker_id}",
        )
        if resp.status_code not in (200, 201):
            raise AssertionError(resp.text)
        return resp.json()

    def seed_project_task(
        self,
        project_id: int,
        task_id: int,
        *,
        title: str,
        status: str = "queued",
        version: int = 1,
        files_to_modify: Optional[list[str]] = None,
        files_to_check: Optional[list[str]] = None,
        implementation_steps: Optional[dict[str, Any]] = None,
        task_type: str = "backend",
    ) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO projects (id, name, status) VALUES (?, ?, 'active')",
                (project_id, f"sandbox-{project_id}"),
            )
            conn.execute(
                """
                INSERT INTO development_tasks
                (id, project_id, title, description, task_type, status, state_version, last_state_change,
                 dependencies, files_to_check, files_to_modify, test_steps, acceptance_criteria, implementation_steps)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    project_id,
                    title,
                    f"{title} description",
                    task_type,
                    status,
                    version,
                    _safe_json([]),
                    _safe_json(files_to_check or []),
                    _safe_json(files_to_modify or []),
                    _safe_json(["pytest -q"]),
                    _safe_json(["passes rehearsal"]),
                    _safe_json(implementation_steps or {}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def set_worker_available(self, worker_id: str) -> None:
        result = WorkerRegistryService(str(self.db_path), v2_enabled=True).set_worker_status(worker_id, "available")
        assert result["success"] is True, result

    def set_worker_status(self, worker_id: str, status: str) -> None:
        result = WorkerRegistryService(str(self.db_path), v2_enabled=True).set_worker_status(worker_id, status)
        assert result["success"] is True, result

    def get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def stop(self) -> None:
        if not self.process:
            return
        proc = self.process
        self.process = None
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                subprocess.run(
                    [_taskkill_exe(), "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass

    def cleanup(self) -> None:
        self.stop()
        gc.collect()
        time.sleep(1.0)
        deadline = time.time() + 15.0
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            try:
                while path.exists() and time.time() < deadline:
                    try:
                        path.unlink()
                    except PermissionError:
                        time.sleep(0.2)
                    except FileNotFoundError:
                        break
                if path.exists():
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
            except Exception:
                pass
        try:
            shutil.rmtree(self.workspace_root, ignore_errors=True)
        except Exception:
            pass


class SandboxRehearsalRunner:
    """Drive a controlled V2 rehearsal against a temporary HTTP backend."""

    def __init__(self, preferred_port: int = 18000):
        self.preferred_port = preferred_port
        self.harness = SandboxBackendHarness(port=preferred_port)
        self.rehearsal_id = f"reh-{uuid.uuid4().hex[:12]}"
        self._prod_snapshot = self._read_prod_snapshot()
        self.report: Dict[str, Any] = {
            "rehearsal_id": self.rehearsal_id,
            "temp_port": self.harness.port,
            "scenarios": {},
            "api_request_count": 0,
            "event_count": 0,
            "final_task_state": "",
            "cleanup": {},
            "formal_db_unchanged": True,
        }

    def _read_prod_snapshot(self) -> Dict[str, int]:
        conn = sqlite3.connect(str(PROD_DB))
        try:
            conn.row_factory = sqlite3.Row
            return {
                "active_runs": self._count(conn, "executor_runs", "status IN ('starting','scanning','claiming','executing','testing','repairing','paused','stopping')"),
                "active_leases": self._count(conn, "task_leases", "status='active'"),
                "active_locks": self._count(conn, "executor_resource_locks", "status='active'"),
            }
        finally:
            conn.close()

    def _count(self, conn: sqlite3.Connection, table: str, where: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {where}").fetchone()
        return int(row["c"] if row else 0)

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        return session

    def _post(self, path: str, body: dict, idem_key: str) -> requests.Response:
        resp = self.harness.request("POST", path, json_body=body, idem_key=idem_key)
        return resp

    def _claim(self, task_id: int, worker_id: str, expected_version: int, key: str, project_id: int) -> dict:
        resp = self._post(
            f"/api/v2/tasks/{task_id}/claim",
            {
                "worker_id": worker_id,
                "expected_version": expected_version,
                "lease_seconds": 300,
                "allowed_task_ids": [task_id],
                "project_id": project_id,
            },
            key,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def _heartbeat(self, task_id: int, assignment_id: str, worker_id: str, lease_token: str, key: str) -> dict:
        resp = self._post(
            f"/api/v2/tasks/{task_id}/heartbeat",
            {
                "assignment_id": assignment_id,
                "worker_id": worker_id,
                "lease_token": lease_token,
                "extend_seconds": 300,
            },
            key,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def _submit_probe(self, task_id: int, claim: dict, sandbox_file: Path, key: str, *, fail_tests: bool = False) -> dict:
        content = sandbox_file.read_bytes()
        sha256 = __import__("hashlib").sha256(content).hexdigest()
        expected_version = int(claim.get("expected_version") or claim.get("state_version") or 1)
        artifact_id = f"artifact-{task_id}-{key}"
        body = {
            "assignment_id": claim["assignment_id"],
            "worker_id": claim["worker_id"],
            "lease_token": claim["lease_token"],
            "expected_version": expected_version,
            "execution_id": f"exec-{task_id}",
            "result_status": "submitted",
            "files_modified": [sandbox_file.name],
            "files_checked": [sandbox_file.name],
            "diff_summary": "probe output generated",
            "tests": {
                "total": 1,
                "passed": 0 if fail_tests else 1,
                "failed": 1 if fail_tests else 0,
                "skipped": 0,
                "output": "probe failed" if fail_tests else "probe ok",
            },
            "git_commit": "0123456789abcdef0123456789abcdef01234567",
            "git_branch": "sandbox/probe",
            "base_commit": "89abcdef0123456789abcdef0123456789abcdef",
            "exit_code": 1 if fail_tests else 0,
            "stdout": "probe stdout",
            "stderr": "" if not fail_tests else "probe stderr",
            "manual_actions": [{"action": "created probe_result.txt", "actor": "connector"}],
            "errors": [] if not fail_tests else [{"message": "probe test failed"}],
            "evidence_refs": [artifact_id],
            "artifacts": [
                {
                    "artifact_id": artifact_id,
                    "artifact_type": "test_report",
                    "uri": f"artifacts/probe/{sandbox_file.name}",
                    "sha256": sha256,
                    "size_bytes": len(content),
                    "mime_type": "text/plain",
                    "metadata": {"probe": True, "worker_id": claim["worker_id"]},
                }
            ],
            "handoff_requested": False,
            "remaining_steps": [],
            "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_ms": 10,
            "model_calls": 0,
            "repair_attempts": 0,
        }
        resp = self._post(f"/api/v2/tasks/{task_id}/submit", body, key)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        data["artifact_id"] = artifact_id
        return data

    def _begin_review(self, task_id: int, result_id: str, reviewer_id: str, expected_version: int, key: str) -> dict:
        resp = self._post(
            f"/api/v2/tasks/{task_id}/review",
            {"action": "begin", "result_id": result_id, "reviewer_id": reviewer_id, "expected_version": expected_version},
            key,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def _decide_review(self, task_id: int, result_id: str, reviewer_id: str, expected_version: int, decision: str, summary: str, issues, evidence_refs, key: str, metadata: Optional[dict] = None) -> dict:
        resp = self._post(
            f"/api/v2/tasks/{task_id}/review",
            {
                "action": "decide",
                "result_id": result_id,
                "reviewer_id": reviewer_id,
                "expected_version": expected_version,
                "decision": decision,
                "summary": summary,
                "issues": issues,
                "evidence_refs": evidence_refs,
                "risk_level": "low",
                "user_action_required": False,
                "metadata": metadata or {},
            },
            key,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def _handoff_request(self, task_id: int, assignment_id: str, from_worker_id: str, lease_token: str, reason_code: str, reason: str, key: str, *, completed_steps=None, remaining_steps=None, evidence_refs=None, forbidden_actions=None, current_stage="implementation", git_head="") -> dict:
        resp = self._post(
            f"/api/v2/tasks/{task_id}/handoff",
            {
                "action": "request",
                "assignment_id": assignment_id,
                "from_worker_id": from_worker_id,
                "lease_token": lease_token,
                "reason_code": reason_code,
                "reason": reason,
                "completed_steps": completed_steps or ["registered", "claimed"],
                "remaining_steps": remaining_steps or ["review"],
                "recent_errors": [],
                "evidence_refs": evidence_refs or ["artifact-probe"],
                "forbidden_actions": forbidden_actions or ["forge_result"],
                "files_changed": ["probe_result.txt"],
                "tests_run": [{"name": "probe", "status": "passed"}],
                "context_snapshot": {"current_stage": current_stage},
                "git_head": git_head or "0123456789abcdef0123456789abcdef01234567",
                "current_stage": current_stage,
                "expires_seconds": 600,
            },
            key,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def _handoff_accept(self, task_id: int, handoff_id: str, to_worker_id: str, expected_version: int, key: str) -> dict:
        resp = self._post(
            f"/api/v2/tasks/{task_id}/handoff",
            {
                "action": "accept",
                "handoff_id": handoff_id,
                "to_worker_id": to_worker_id,
                "expected_version": expected_version,
                "lease_seconds": 300,
            },
            key,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def _inspect_events(self) -> int:
        conn = self.harness.get_conn()
        try:
            row = conn.execute("SELECT COUNT(*) AS c FROM task_events").fetchone()
            return int(row["c"] if row else 0)
        finally:
            conn.close()

    def _task_state(self, task_id: int) -> tuple[str, int]:
        conn = self.harness.get_conn()
        try:
            row = conn.execute("SELECT status, state_version FROM development_tasks WHERE id=?", (task_id,)).fetchone()
            return str(row["status"]), int(row["state_version"])
        finally:
            conn.close()

    def _check_prod_snapshot_unchanged(self) -> bool:
        current = self._read_prod_snapshot()
        return current == self._prod_snapshot

    def run(self) -> Dict[str, Any]:
        self.harness.start()
        try:
            self._run_happy_path()
            self._run_rework_handoff_path()
            self._run_quota_and_expiry_paths()
            self._run_need_user_and_blocked_paths()
            self._run_conflict_paths()
            self.report["event_count"] = self._inspect_events()
            self.report["api_request_count"] = self.harness.api_request_count
            self.report["formal_db_unchanged"] = self._check_prod_snapshot_unchanged()
            self.report["final_task_state"] = self._task_state(self._last_task_id)[0].upper()
            return self.report
        finally:
            self.harness.cleanup()
            cleanup = {
                "backend_stopped": True,
                "db_removed": not self.harness.db_path.exists(),
                "workspace_removed": not self.harness.workspace_root.exists(),
            }
            if not cleanup["db_removed"] or not cleanup["workspace_removed"]:
                time.sleep(3.0)
                gc.collect()
                self.harness.cleanup()
                cleanup = {
                    "backend_stopped": True,
                    "db_removed": not self.harness.db_path.exists(),
                    "workspace_removed": not self.harness.workspace_root.exists(),
                }
            self.report["cleanup"] = cleanup

    def _run_happy_path(self) -> None:
        project_id = 9100
        task_id = 9101
        self._last_task_id = task_id
        self.harness.seed_project_task(
            project_id,
            task_id,
            title="happy-path",
            status="queued",
            version=1,
            files_to_modify=["probe_result.txt"],
            files_to_check=["probe_result.txt"],
            implementation_steps={"_requirements": {"language": "python"}},
        )
        self.harness.register_worker("exec-a", "executor", capabilities=["python"])
        self.harness.register_worker("rev-a", "reviewer")
        self.harness.set_worker_available("exec-a")
        plan = SupervisorOrchestrationService(str(self.harness.db_path), v2_enabled=True).run_one_cycle(project_id, "cycle-happy-dry", dry_run=True)
        assert plan["planned_action"] == "CLAIM_TASK"
        live = SupervisorOrchestrationService(str(self.harness.db_path), v2_enabled=True).run_one_cycle(project_id, "cycle-happy-live", dry_run=False)
        assert live["success"] is True
        conn = self.harness.get_conn()
        try:
            assignment = conn.execute("SELECT assignment_id, lease_token FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
        finally:
            conn.close()
        assert assignment is not None
        heartbeat = self._heartbeat(task_id, assignment["assignment_id"], "exec-a", assignment["lease_token"], "hb-happy")
        assert heartbeat["idempotent"] is False
        TaskStateMachineService(str(self.harness.db_path), v2_enabled=True).transition(
            task_id,
            "RUNNING",
            "supervisor",
            reason="worker started execution",
            expected_version=2,
            idempotency_key="state-happy-running",
        )
        conn = self.harness.get_conn()
        try:
            conn.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (assignment["assignment_id"],))
            conn.commit()
        finally:
            conn.close()
        sandbox_file = self.harness.workspace_root / "probe_result.txt"
        sandbox_file.write_text("probe ok for happy path", encoding="utf-8")
        submit = self._submit_probe(task_id, {"assignment_id": assignment["assignment_id"], "worker_id": "exec-a", "lease_token": assignment["lease_token"], "state_version": 3}, sandbox_file, "submit-happy")
        assert submit["task_state"] == "RESULT_SUBMITTED"
        begin = self._begin_review(task_id, submit["result_id"], "rev-a", 4, "review-begin-happy")
        assert begin["task_state"] == "REVIEWING"
        decide = self._decide_review(task_id, submit["result_id"], "rev-a", 5, "VERIFIED", "happy path verified", [], [submit["artifact_id"]], "review-decide-happy")
        assert decide["task_state"] == "VERIFIED"
        final = SupervisorOrchestrationService(str(self.harness.db_path), v2_enabled=True).run_one_cycle(project_id, "cycle-happy-final", dry_run=True)
        assert final["planned_action"] == "NO_ACTION"
        self.harness.set_worker_status("exec-a", "registered")
        self.report["scenarios"]["happy_path"] = {
            "success": True,
            "task_state": self._task_state(task_id)[0].upper(),
            "requests": self.harness.api_request_count,
        }

    def _run_rework_handoff_path(self) -> None:
        project_id = 9101
        task_id = 9102
        self._last_task_id = task_id
        self.harness.seed_project_task(
            project_id,
            task_id,
            title="rework-path",
            status="queued",
            version=1,
            files_to_modify=["probe_result.txt"],
            files_to_check=["probe_result.txt"],
            implementation_steps={"_requirements": {"language": "python"}},
        )
        self.harness.set_worker_status("exec-a", "registered")
        self.harness.register_worker("exec-b", "executor", capabilities=["python"])
        self.harness.register_worker("rev-b", "reviewer")
        self.harness.set_worker_available("exec-b")
        claim = self._claim(task_id, "exec-b", 1, "claim-rework", project_id)
        TaskStateMachineService(str(self.harness.db_path), v2_enabled=True).transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-rework-running")
        conn = self.harness.get_conn()
        try:
            conn.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (claim["assignment_id"],))
            conn.commit()
        finally:
            conn.close()
        sandbox_file = self.harness.workspace_root / "probe_result.txt"
        sandbox_file.write_text("probe needs rework", encoding="utf-8")
        submit = self._submit_probe(task_id, {"assignment_id": claim["assignment_id"], "worker_id": "exec-b", "lease_token": claim["lease_token"], "expected_version": 3}, sandbox_file, "submit-rework", fail_tests=True)
        begin = self._begin_review(task_id, submit["result_id"], "rev-b", 4, "review-begin-rework")
        assert begin["task_state"] == "REVIEWING"
        review = self._decide_review(
            task_id,
            submit["result_id"],
            "rev-b",
            5,
            "REWORK",
            "needs more tests",
            [{"severity": "high", "reason": "missing coverage", "acceptance": "add coverage", "suggested_fix": "expand tests"}],
            [submit["artifact_id"]],
            "review-decide-rework",
        )
        assert review["task_state"] == "REWORK"
        self.harness.set_worker_status("exec-b", "registered")
        self.harness.register_worker("exec-c", "executor", capabilities=["python"])
        self.harness.set_worker_available("exec-c")
        handoff = self._handoff_request(
            task_id,
            claim["assignment_id"],
            "exec-b",
            claim["lease_token"],
            "REWORK_REQUIRED",
            "reviewer requested rework",
            "handoff-rework",
            completed_steps=["implemented", "submitted"],
            remaining_steps=["fix tests", "resubmit"],
            evidence_refs=[submit["artifact_id"]],
        )
        accepted = self._handoff_accept(task_id, handoff["handoff_id"], "exec-c", 6, "handoff-accept-rework")
        conn = self.harness.get_conn()
        try:
            new_assignment = conn.execute("SELECT assignment_id, lease_token, worker_id FROM task_assignments WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        finally:
            conn.close()
        assert new_assignment["worker_id"] == "exec-c"
        sandbox_file.write_text("probe fixed after handoff", encoding="utf-8")
        conn = self.harness.get_conn()
        try:
            conn.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (new_assignment["assignment_id"],))
            conn.commit()
        finally:
            conn.close()
        submit2 = self._submit_probe(task_id, {"assignment_id": new_assignment["assignment_id"], "worker_id": "exec-c", "lease_token": new_assignment["lease_token"], "expected_version": 7}, sandbox_file, "submit-rework-final")
        self._begin_review(task_id, submit2["result_id"], "rev-b", 8, "review-begin-rework-final")
        final = self._decide_review(task_id, submit2["result_id"], "rev-b", 9, "VERIFIED", "rework resolved", [], [submit2["artifact_id"]], "review-decide-rework-final")
        assert final["task_state"] == "VERIFIED"
        self.harness.set_worker_status("exec-b", "registered")
        self.harness.set_worker_status("exec-c", "registered")
        self.report["scenarios"]["rework_handoff"] = {
            "success": True,
            "handoff_status": accepted["status"],
            "task_state": self._task_state(task_id)[0].upper(),
        }

    def _run_quota_and_expiry_paths(self) -> None:
        project_id = 9102
        task_id = 9103
        self._last_task_id = task_id
        self.harness.seed_project_task(
            project_id,
            task_id,
            title="quota-and-expiry",
            status="queued",
            version=1,
            files_to_modify=["probe_result.txt"],
            files_to_check=["probe_result.txt"],
        )
        self.harness.register_worker("exec-d", "executor")
        self.harness.register_worker("exec-e", "executor")
        self.harness.set_worker_available("exec-d")
        claim = self._claim(task_id, "exec-d", 1, "claim-quota", project_id)
        TaskStateMachineService(str(self.harness.db_path), v2_enabled=True).transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-quota-running")
        conn = self.harness.get_conn()
        try:
            conn.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (claim["assignment_id"],))
            conn.commit()
        finally:
            conn.close()
        sandbox_file = self.harness.workspace_root / "probe_result.txt"
        sandbox_file.write_text("quota probe", encoding="utf-8")
        handoff = self._handoff_request(
            task_id,
            claim["assignment_id"],
            "exec-d",
            claim["lease_token"],
            "QUOTA_EXHAUSTED",
            "worker exhausted quota",
            "handoff-quota",
            completed_steps=["step1"],
            remaining_steps=["step2"],
            evidence_refs=["artifact-quota"],
        )
        self.harness.set_worker_status("exec-d", "registered")
        self.harness.set_worker_available("exec-e")
        accepted = self._handoff_accept(task_id, handoff["handoff_id"], "exec-e", 3, "handoff-accept-quota")
        assert accepted["status"] == "accepted"
        self.harness.set_worker_status("exec-d", "registered")
        self.harness.set_worker_status("exec-e", "registered")
        self.harness.register_worker("exec-f", "executor")
        self.harness.set_worker_available("exec-f")

        expiry_task = 9104
        self.harness.seed_project_task(project_id, expiry_task, title="lease-expiry", status="queued", version=1, files_to_modify=["probe_result.txt"], files_to_check=["probe_result.txt"])
        claim2 = self._claim(expiry_task, "exec-f", 1, "claim-expiry", project_id)
        conn = self.harness.get_conn()
        try:
            expired = (datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE task_assignments SET lease_expires_at=? WHERE assignment_id=?", (expired, claim2["assignment_id"]))
            conn.commit()
        finally:
            conn.close()
        rec = LeaseRecoveryService(str(self.harness.db_path), v2_enabled=True).recover_assignment(claim2["assignment_id"], "expired lease", "recover-expiry")
        assert rec["task_state"] == "QUEUED"

        running_task = 9105
        self.harness.seed_project_task(project_id, running_task, title="running-expiry", status="queued", version=1, files_to_modify=["probe_result.txt"], files_to_check=["probe_result.txt"])
        self.harness.set_worker_available("exec-f")
        claim3 = self._claim(running_task, "exec-f", 1, "claim-running-expiry", project_id)
        TaskStateMachineService(str(self.harness.db_path), v2_enabled=True).transition(running_task, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-running-expiry")
        conn = self.harness.get_conn()
        try:
            conn.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (claim3["assignment_id"],))
            conn.execute("UPDATE task_assignments SET lease_expires_at=? WHERE assignment_id=?", ((datetime.now() - timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"), claim3["assignment_id"]))
            conn.commit()
        finally:
            conn.close()
        rec2 = LeaseRecoveryService(str(self.harness.db_path), v2_enabled=True).recover_assignment(claim3["assignment_id"], "lease expired during execution", "recover-running")
        assert rec2["task_state"] == "BLOCKED"
        self.harness.set_worker_status("exec-f", "registered")

        self.report["scenarios"]["quota_and_expiry"] = {
            "success": True,
            "quota_status": accepted["status"],
            "claim_expiry_state": rec["task_state"],
            "running_expiry_state": rec2["task_state"],
        }

    def _run_need_user_and_blocked_paths(self) -> None:
        project_id = 9103
        task_id = 9106
        self._last_task_id = task_id
        self.harness.seed_project_task(project_id, task_id, title="need-user", status="queued", version=1, files_to_modify=["probe_result.txt"], files_to_check=["probe_result.txt"])
        self.harness.register_worker("exec-g", "executor")
        self.harness.register_worker("rev-c", "reviewer")
        self.harness.set_worker_available("exec-g")
        claim = self._claim(task_id, "exec-g", 1, "claim-user", project_id)
        TaskStateMachineService(str(self.harness.db_path), v2_enabled=True).transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-user-running")
        conn = self.harness.get_conn()
        try:
            conn.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (claim["assignment_id"],))
            conn.commit()
        finally:
            conn.close()
        sandbox_file = self.harness.workspace_root / "probe_result.txt"
        sandbox_file.write_text("need user probe", encoding="utf-8")
        submit = self._submit_probe(task_id, {"assignment_id": claim["assignment_id"], "worker_id": "exec-g", "lease_token": claim["lease_token"], "state_version": 3}, sandbox_file, "submit-user")
        self._begin_review(task_id, submit["result_id"], "rev-c", 4, "review-begin-user")
        need_user = self._decide_review(
            task_id,
            submit["result_id"],
            "rev-c",
            5,
            "NEED_USER",
            "needs user input",
            [{"question": "Which option should we take?", "options": ["A", "B"], "risk": "medium"}],
            [submit["artifact_id"]],
            "review-decide-user",
        )
        assert need_user["task_state"] == "NEED_USER"
        self.harness.set_worker_status("exec-g", "registered")

        blocked_project = 9104
        blocked_task = 9107
        self.harness.seed_project_task(blocked_project, blocked_task, title="blocked", status="blocked", version=1, files_to_modify=["probe_result.txt"], files_to_check=["probe_result.txt"])
        plan = SupervisorOrchestrationService(str(self.harness.db_path), v2_enabled=True).run_one_cycle(blocked_project, "cycle-blocked", dry_run=True)
        assert plan["planned_action"] == "STOP_AND_REPORT_BLOCKER"
        self.report["scenarios"]["need_user_and_blocked"] = {"success": True, "need_user": need_user["task_state"], "blocked_action": plan["planned_action"]}

    def _run_conflict_paths(self) -> None:
        project_id = 9105
        task_id = 9108
        self._last_task_id = task_id
        self.harness.seed_project_task(project_id, task_id, title="conflicts", status="queued", version=1, files_to_modify=["probe_result.txt"], files_to_check=["probe_result.txt"], implementation_steps={"_requirements": {"language": "python"}})
        self.harness.register_worker("exec-h", "executor", capabilities=["python"])
        self.harness.register_worker("exec-i", "executor", capabilities=["python"])
        self.harness.set_worker_available("exec-h")
        claim = self._claim(task_id, "exec-h", 1, "claim-conflict", project_id)
        TaskStateMachineService(str(self.harness.db_path), v2_enabled=True).transition(task_id, "RUNNING", "supervisor", reason="worker started execution", expected_version=2, idempotency_key="state-conflict-running")
        conn = self.harness.get_conn()
        try:
            conn.execute("UPDATE task_assignments SET status='running' WHERE assignment_id=?", (claim["assignment_id"],))
            conn.commit()
        finally:
            conn.close()
        sandbox_file = self.harness.workspace_root / "probe_result.txt"
        sandbox_file.write_text("conflict probe", encoding="utf-8")
        submit = self._submit_probe(task_id, {"assignment_id": claim["assignment_id"], "worker_id": "exec-h", "lease_token": claim["lease_token"], "state_version": 3}, sandbox_file, "submit-conflict")
        # stale expected version / bad token / scope violations are exercised through HTTP rejections
        bad_version = self._post(
            f"/api/v2/tasks/{task_id}/submit",
            {
                "assignment_id": claim["assignment_id"],
                "worker_id": "exec-h",
                "lease_token": "wrong-token",
                "expected_version": 99,
                "execution_id": "exec-bad",
                "result_status": "submitted",
                "files_modified": [sandbox_file.name],
                "files_checked": [sandbox_file.name],
                "tests": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "output": "ok"},
                "git_commit": "0123456789abcdef0123456789abcdef01234567",
                "manual_actions": [],
                "errors": [],
                "evidence_refs": [submit["artifact_id"]],
                "artifacts": [],
                "handoff_requested": False,
                "remaining_steps": [],
                "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "submit-conflict-bad",
        )
        assert bad_version.status_code in (409, 422)
        scope_violation = self._post(
            f"/api/v2/tasks/{task_id}/submit",
            {
                "assignment_id": claim["assignment_id"],
                "worker_id": "exec-i",
                "lease_token": claim["lease_token"],
                "expected_version": 3,
                "execution_id": "exec-scope",
                "result_status": "submitted",
                "files_modified": [sandbox_file.name],
                "files_checked": [sandbox_file.name],
                "tests": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "output": "ok"},
                "git_commit": "0123456789abcdef0123456789abcdef01234567",
                "manual_actions": [],
                "errors": [],
                "evidence_refs": [submit["artifact_id"]],
                "artifacts": [],
                "handoff_requested": False,
                "remaining_steps": [],
                "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "submit-conflict-scope",
        )
        assert scope_violation.status_code in (403, 404, 409, 422)
        self.report["scenarios"]["conflicts"] = {
            "success": True,
            "submit_status": submit["task_state"],
            "bad_token_http": bad_version.status_code,
            "scope_http": scope_violation.status_code,
        }


def run_rehearsal(preferred_port: int = 18000) -> Dict[str, Any]:
    return SandboxRehearsalRunner(preferred_port=preferred_port).run()


def main(argv: Optional[Iterable[str]] = None) -> int:
    _ = argv
    report = run_rehearsal()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("formal_db_unchanged") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
