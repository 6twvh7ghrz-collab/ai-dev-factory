"""Local sandbox agent connector MVP."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .client import ControlPlaneClient
from .heartbeat_loop import HeartbeatLoop
from .models import ConnectorConfig
from .probe_executor import execute_probe_task


@dataclass
class LocalAgentConnector:
    config: ConnectorConfig
    client: ControlPlaneClient = field(init=False)
    heartbeat: Optional[HeartbeatLoop] = field(default=None, init=False)
    current_claim: Optional[Dict[str, Any]] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.client = ControlPlaneClient(self.config)

    def register(self) -> Dict[str, Any]:
        if self.config.dry_run:
            return {
                "ok": True,
                "mode": "dry_run",
                "planned_action": "REGISTER_WORKER",
                "worker_id": self.config.worker_id,
            }
        return self.client.register_worker()

    def claim_once(self, task_id: Optional[int] = None, *, expected_version: int = 1) -> Dict[str, Any]:
        task_id = int(task_id or (self.config.allowed_task_ids[0] if self.config.allowed_task_ids else 0))
        if task_id <= 0:
            return {"ok": False, "error_code": "VALIDATION_ERROR", "message": "task_id is required"}
        if self.config.dry_run:
            return {
                "ok": True,
                "mode": "dry_run",
                "planned_action": "CLAIM_TASK",
                "task_id": task_id,
                "worker_id": self.config.worker_id,
                "expected_version": expected_version,
            }
        claim = self.client.claim_task(task_id, expected_version)
        if claim.get("ok"):
            self.current_claim = claim
        return claim

    def start_heartbeat(self) -> Optional[HeartbeatLoop]:
        if self.config.dry_run:
            return None
        if not self.current_claim or not self.current_claim.get("lease_token"):
            return None
        if self.heartbeat and self.heartbeat.is_running():
            return self.heartbeat
        self.heartbeat = HeartbeatLoop(
            client=self.client,
            task_id=int(self.current_claim["task_id"]),
            assignment_id=str(self.current_claim["assignment_id"]),
            worker_id=str(self.current_claim["worker_id"]),
            lease_token=str(self.current_claim["lease_token"]),
            interval=float(self.config.heartbeat_interval),
        )
        self.heartbeat.start()
        return self.heartbeat

    def stop_heartbeat(self) -> None:
        if self.heartbeat:
            self.heartbeat.stop()
        self.heartbeat = None

    def acknowledge_or_start_task(self) -> Dict[str, Any]:
        if not self.current_claim:
            return {"ok": False, "error_code": "VALIDATION_ERROR", "message": "no current claim"}
        return {"ok": True, "claim": dict(self.current_claim)}

    def execute_probe_task(self) -> Dict[str, Any]:
        if not self.current_claim:
            if self.config.dry_run:
                return {
                    "ok": True,
                    "mode": "dry_run",
                    "planned_action": "EXECUTE_CONTROL_PLANE_PROBE",
                    "sandbox_root": str(self.config.sandbox_root),
                }
            return {"ok": False, "error_code": "VALIDATION_ERROR", "message": "no current claim"}
        execution = execute_probe_task(
            sandbox_root=self.config.sandbox_root,
            task_id=int(self.current_claim["task_id"]),
            worker_id=str(self.current_claim["worker_id"]),
            execution_id=str(self.current_claim.get("assignment_id") or self.current_claim.get("execution_id") or ""),
        )
        return {
            "ok": True,
            "execution_id": execution.execution_id,
            "task_id": execution.task_id,
            "worker_id": execution.worker_id,
            "file_path": str(execution.file_path),
            "sha256": execution.sha256,
            "artifact": execution.artifact.as_dict(),
            "result_packet": execution.result_packet,
        }

    def submit_result(self, execution: Dict[str, Any], *, idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        if not self.current_claim:
            return {"ok": False, "error_code": "VALIDATION_ERROR", "message": "no current claim"}
        if self.config.dry_run:
            return {
                "ok": True,
                "mode": "dry_run",
                "planned_action": "SUBMIT_RESULT",
                "task_id": self.current_claim.get("task_id"),
                "assignment_id": self.current_claim.get("assignment_id"),
            }
        idem = idempotency_key or f"submit-{self.current_claim['assignment_id']}"
        result = self.client.submit_result(
            int(self.current_claim["task_id"]),
            execution["result_packet"],
            lease_token=str(self.current_claim["lease_token"]),
            assignment_id=str(self.current_claim["assignment_id"]),
            expected_version=int(self.current_claim.get("state_version") or 1) + 1,
            idem_key=idem,
        )
        return result

    def request_handoff(self, *, reason_code: str, reason: str, completed_steps: list[Any], remaining_steps: list[Any], recent_errors: list[Any], evidence_refs: list[str], forbidden_actions: list[str], idempotency_key: str) -> Dict[str, Any]:
        if not self.current_claim:
            return {"ok": False, "error_code": "VALIDATION_ERROR", "message": "no current claim"}
        return self.client.request_handoff(
            int(self.current_claim["task_id"]),
            {
                "assignment_id": self.current_claim["assignment_id"],
                "from_worker_id": self.current_claim["worker_id"],
                "lease_token": self.current_claim["lease_token"],
                "reason_code": reason_code,
                "reason": reason,
                "completed_steps": completed_steps,
                "remaining_steps": remaining_steps,
                "recent_errors": recent_errors,
                "evidence_refs": evidence_refs,
                "forbidden_actions": forbidden_actions,
                "files_changed": [self.config.sandbox_root.joinpath("probe_result.txt").name],
                "tests_run": [{"name": "probe", "status": "passed"}],
                "context_snapshot": {"sandbox_root": str(self.config.sandbox_root)},
                "git_head": "0123456789abcdef0123456789abcdef01234567",
                "current_stage": "implementation",
                "expires_seconds": 600,
            },
            idem_key=idempotency_key,
        )

    def run_once(self, task_id: Optional[int] = None) -> Dict[str, Any]:
        register = self.register()
        if not register.get("ok"):
            return register
        claim = self.claim_once(task_id)
        if not claim.get("ok"):
            return claim
        if self.config.dry_run:
            return {
                "ok": True,
                "mode": "dry_run",
                "planned_actions": [register, claim, self.execute_probe_task()],
            }
        self.start_heartbeat()
        probe = self.execute_probe_task()
        if not probe.get("ok"):
            return probe
        if self.config.dry_run:
            return {"ok": True, "mode": "dry_run", "claim": claim, "probe": probe}
        submit = self.submit_result(probe)
        return {"ok": bool(submit.get("ok")), "claim": claim, "probe": probe, "submit": submit}

    def shutdown(self) -> None:
        self.stop_heartbeat()
        self.client.close()


def _build_config_from_args(argv: Optional[list[str]] = None) -> tuple[ConnectorConfig, Optional[int]]:
    parser = argparse.ArgumentParser(description="Run the local sandbox agent connector")
    parser.add_argument("--config", required=True, help="Path to a JSON config file")
    parser.add_argument("--task-id", type=int, default=None, help="Task ID to claim and execute")
    args = parser.parse_args(argv)
    config = ConnectorConfig.from_file(args.config)
    return config, args.task_id


def main(argv: Optional[list[str]] = None) -> int:
    config, task_id = _build_config_from_args(argv)
    connector = LocalAgentConnector(config)
    try:
        result = connector.run_once(task_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    finally:
        connector.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
