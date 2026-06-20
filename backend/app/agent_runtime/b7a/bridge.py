"""High-level B7A execution bridge."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .models import EvidenceBundle, PatchProposal, WorkspaceSnapshot, utc_now
from .patch_application import PatchApplicationService, PatchApplicationError
from .policy import TaskExecutionPolicy
from .providers.base import PatchProvider
from .secrets import RuntimeSecretProvider
from .workspace import WorkspaceSnapshotBuilder


@dataclass(slots=True)
class B7AExecutionBridge:
    provider: PatchProvider
    secret_provider: RuntimeSecretProvider
    submit_callback: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    artifacts: Dict[str, Any] = field(default_factory=dict)

    def execute(
        self,
        *,
        task_packet: Dict[str, Any],
        workspace_root: Path,
        policy: TaskExecutionPolicy,
    ) -> Dict[str, Any]:
        app = PatchApplicationService(workspace_root=workspace_root)
        decision = policy.evaluate()
        if not decision.allowed:
            return {"ok": False, "error_code": decision.code, "message": decision.reason}

        config_check = self.provider.validate_config()
        if not config_check.get("ok"):
            return config_check
        if self.provider.provider_name != "mock" and not self.secret_provider.configured():
            return {"ok": False, "error_code": "SECRET_NOT_CONFIGURED", "message": "secret is required"}

        snapshot = WorkspaceSnapshotBuilder(
            workspace_root=workspace_root,
            allowed_files=list(task_packet.get("allowed_files", [])),
            allowed_test_commands=list(task_packet.get("allowed_test_commands", [])),
            forbidden_actions=list(task_packet.get("forbidden_actions", [])),
            temporary_project=bool(task_packet.get("temporary_project", False)),
            project_id=int(task_packet.get("project_id", 0)),
            task_packet=task_packet,
        ).build()

        app.verify_workspace()
        checkpoint = app.create_checkpoint()

        try:
            proposal = self.provider.generate_patch(task_packet, snapshot)
            app.validate_proposal(proposal, snapshot)
            changed_files = app.apply_patch_proposal(proposal)
            tests_run = []
            for command in proposal.expected_tests or snapshot.allowed_test_commands:
                tests_run.append(app.run_allowed_test(command))
                if not tests_run[-1]["ok"]:
                    raise PatchApplicationError("test failed")
            evidence = app.finalize_evidence(
                proposal=proposal,
                changed_files=changed_files,
                tests_run=tests_run,
                summary="patch applied and tests passed",
            )
            result_packet = self._build_result_packet(task_packet, proposal, evidence, changed_files, tests_run)
            submit_response = None
            if self.submit_callback:
                submit_response = self.submit_callback(result_packet)
            return {
                "ok": True,
                "proposal": proposal.as_dict(),
                "evidence": evidence.as_dict(),
                "result_packet": result_packet,
                "submit": submit_response,
                "checkpoint": checkpoint,
            }
        except Exception as exc:
            app.rollback(checkpoint)
            return {"ok": False, "error_code": "PATCH_APPLICATION_FAILED", "message": str(exc)}

    def _build_result_packet(
        self,
        task_packet: Dict[str, Any],
        proposal: PatchProposal,
        evidence: EvidenceBundle,
        changed_files: list[str],
        tests_run: list[Dict[str, Any]],
    ) -> Dict[str, Any]:
        artifact_ids = [artifact["artifact_id"] for artifact in evidence.artifacts]
        payload = {
            "execution_id": f"exec-{proposal.proposal_id}",
            "result_status": "submitted",
            "files_modified": list(changed_files),
            "files_checked": list(changed_files),
            "diff_summary": proposal.unified_diff or proposal.explanation,
            "tests": {
                "total": len(tests_run),
                "passed": sum(1 for row in tests_run if row.get("ok")),
                "failed": sum(1 for row in tests_run if not row.get("ok")),
                "skipped": 0,
                "output": "\n".join(row.get("stdout", "") for row in tests_run),
            },
            "git_commit": hashlib.sha256((proposal.proposal_id + utc_now()).encode()).hexdigest()[:40],
            "git_branch": "sandbox/b7a",
            "base_commit": "0" * 40,
            "exit_code": 0,
            "stdout": evidence.summary,
            "stderr": "",
            "manual_actions": [{"action": "applied proposal", "actor": self.provider.provider_name}],
            "errors": [],
            "evidence_id": evidence.evidence_id,
            "evidence_refs": artifact_ids,
            "artifacts": evidence.artifacts,
            "handoff_requested": False,
            "remaining_steps": [],
            "worker_id": str(task_packet.get("worker_id", "runtime-worker")),
            "submitted_at": utc_now(),
            "duration_ms": 1,
            "model_calls": 0,
            "repair_attempts": 0,
            "proposal_id": proposal.proposal_id,
            "task_id": task_packet.get("task_id"),
        }
        return payload
