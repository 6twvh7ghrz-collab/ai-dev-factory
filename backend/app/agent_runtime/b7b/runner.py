"""B7B safe ignition runner for single-file patching drills."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.agent_runtime.b7a import (
    MockProvider,
    PatchApplicationService,
    PatchFile,
    PatchProposal,
    TaskExecutionPolicy,
    WorkspaceSnapshotBuilder,
)
from app.agent_runtime.b7a.patch_application import PatchApplicationError
from app.agent_runtime.b7a.models import utc_now


@dataclass(slots=True)
class B7BStepResult:
    step: str
    ok: bool
    code: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class B7BReport:
    report_id: str
    created_at: str
    repo_root: str
    steps: List[B7BStepResult]
    summary: Dict[str, Any]
    report_path: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.as_dict() for step in self.steps]
        return payload

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.report_id:
            errors.append("missing report_id")
        if not self.steps:
            errors.append("missing steps")
        return errors

    def write_to(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self.report_path = str(path)
        return path


class B7BDiffReviewService:
    def review(self, proposal: PatchProposal, allowed_files: List[str]) -> B7BStepResult:
        if len(proposal.files) != 1:
            return B7BStepResult(
                step="diff_review",
                ok=False,
                code="DIFF_REVIEW_DENIED",
                message="B7B requires a single-file patch",
                details={"files": [item.relative_path for item in proposal.files]},
            )
        file_item = proposal.files[0]
        if file_item.relative_path not in set(allowed_files):
            return B7BStepResult(
                step="diff_review",
                ok=False,
                code="DIFF_REVIEW_DENIED",
                message="proposal targets a disallowed file",
                details={"relative_path": file_item.relative_path},
            )
        path = Path(file_item.relative_path)
        parts = {part.lower() for part in path.parts}
        if file_item.relative_path.startswith(("/", "\\\\")) or ".." in path.parts:
            return B7BStepResult(
                step="diff_review",
                ok=False,
                code="DIFF_REVIEW_DENIED",
                message="proposal path is invalid",
                details={"relative_path": file_item.relative_path},
            )
        if ".git" in parts or ".env" in parts:
            return B7BStepResult(
                step="diff_review",
                ok=False,
                code="DIFF_REVIEW_DENIED",
                message="proposal targets forbidden repository content",
                details={"relative_path": file_item.relative_path},
            )
        if path.suffix.lower() in {".env", ".db", ".sqlite", ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".dll", ".exe", ".bin"}:
            return B7BStepResult(
                step="diff_review",
                ok=False,
                code="DIFF_REVIEW_DENIED",
                message="proposal targets forbidden file types",
                details={"relative_path": file_item.relative_path},
            )
        return B7BStepResult(
            step="diff_review",
            ok=True,
            code="DIFF_REVIEW_APPROVED",
            message="proposal approved",
            details={"relative_path": file_item.relative_path, "unified_diff": proposal.unified_diff},
        )


class _BrokenProvider(MockProvider):
    def generate_patch(self, task_packet: Dict[str, Any], workspace_snapshot):
        proposal = super().generate_patch(task_packet, workspace_snapshot)
        proposal.files = [
            PatchFile(
                relative_path="calculator.py",
                operation="modify",
                expected_sha256=workspace_snapshot.file_hashes["calculator.py"],
                new_content="def add(a, b):\n    return a + 1\n",
                encoding="utf-8",
            )
        ]
        proposal.unified_diff = "--- a/calculator.py\n+++ b/calculator.py\n@@\n-    return a - b\n+    return a + 1\n"
        proposal.explanation = "deliberately broken patch for rollback drill"
        return proposal


class _UnauthorizedProvider(MockProvider):
    def generate_patch(self, task_packet: Dict[str, Any], workspace_snapshot):
        proposal = super().generate_patch(task_packet, workspace_snapshot)
        proposal.files = [
            PatchFile(
                relative_path="test_calculator.py",
                operation="modify",
                expected_sha256=workspace_snapshot.file_hashes["calculator.py"],
                new_content="assert False\n",
                encoding="utf-8",
            )
        ]
        proposal.unified_diff = "--- a/test_calculator.py\n+++ b/test_calculator.py\n@@\n+assert False\n"
        proposal.explanation = "out-of-scope patch for unauthorized verification"
        return proposal


class B7BSafeIgnitionRunner:
    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root)
        self.reviewer = B7BDiffReviewService()

    def run(self, *, report_path: Optional[Path] = None) -> B7BReport:
        service = PatchApplicationService(self.workspace_root)
        service.verify_workspace()
        checkpoint = service.create_checkpoint()
        report_steps: List[B7BStepResult] = []
        summary: Dict[str, Any] = {}

        try:
            snapshot = WorkspaceSnapshotBuilder(
                workspace_root=self.workspace_root,
                allowed_files=["calculator.py"],
                allowed_test_commands=["python -m pytest test_calculator.py"],
                forbidden_actions=["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
                temporary_project=True,
                project_id=9300,
                task_packet={
                    "task_id": 9301,
                    "project_id": 9300,
                    "task_type": "SANDBOX_CODE_PATCH",
                    "temporary_project": True,
                    "allowed_files": ["calculator.py"],
                    "allowed_test_commands": ["python -m pytest test_calculator.py"],
                    "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
                    "max_files_changed": 1,
                    "max_patch_bytes": 1024,
                    "evidence_required": True,
                    "worker_id": "b7b-worker",
                    "approval_token": "one-time",
                    "mode": "mock",
                    "sandbox_root": str(self.workspace_root),
                    "control_plane_url": "http://127.0.0.1:8000",
                },
            ).build()

            policy = TaskExecutionPolicy(
                task_type="SANDBOX_CODE_PATCH",
                temporary_project=True,
                project_id=9300,
                sandbox_root=str(self.workspace_root),
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
            policy_decision = policy.evaluate()
            report_steps.append(
                B7BStepResult(
                    step="policy_gate",
                    ok=policy_decision.allowed,
                    code=policy_decision.code,
                    message=policy_decision.reason,
                    details=policy_decision.details,
                )
            )
            if not policy_decision.allowed:
                return self._finalize(report_steps, summary, report_path)

            provider = MockProvider()
            proposal = provider.generate_patch(snapshot.task_packet, snapshot)
            report_steps.append(
                B7BStepResult(
                    step="ai_patch",
                    ok=True,
                    code="PATCH_PROPOSAL_CREATED",
                    message="mock provider generated a patch proposal",
                    details={"proposal_id": proposal.proposal_id, "files": [f.relative_path for f in proposal.files]},
                )
            )

            review = self.reviewer.review(proposal, ["calculator.py"])
            report_steps.append(review)
            if not review.ok:
                return self._finalize(report_steps, summary, report_path)

            service.validate_proposal(proposal, snapshot)
            changed = service.apply_patch_proposal(proposal)
            test_result = service.run_allowed_test("python -m pytest test_calculator.py")
            if not test_result["ok"]:
                raise PatchApplicationError("happy path test failed")
            evidence = service.finalize_evidence(
                proposal=proposal,
                changed_files=changed,
                tests_run=[test_result],
                summary="happy path patch verified",
            )
            summary["happy_path"] = {
                "changed_files": changed,
                "proposal_id": proposal.proposal_id,
                "evidence_id": evidence.evidence_id,
            }
            report_steps.append(
                B7BStepResult(
                    step="apply_and_verify",
                    ok=True,
                    code="PATCH_VERIFIED",
                    message="single-file bug fixed and tested",
                    details={
                        "changed_files": changed,
                        "test_command": test_result["command"],
                        "artifact_ids": [artifact["artifact_id"] for artifact in evidence.artifacts],
                    },
                )
            )
            service.rollback(checkpoint)
            report_steps.append(
                B7BStepResult(
                    step="restore_checkpoint",
                    ok=True,
                    code="ROLLBACK_OK",
                    message="workspace restored after happy path verification",
                    details={"checkpoint": checkpoint},
                )
            )

            rollback_result = self._run_rollback_drill()
            report_steps.append(rollback_result)

            unauthorized_result = self._run_unauthorized_verification()
            report_steps.append(unauthorized_result)
            summary["unauthorized_gate"] = unauthorized_result.details
            summary["rollback_drill"] = rollback_result.details

            return self._finalize(report_steps, summary, report_path)
        except Exception as exc:
            service.rollback(checkpoint)
            report_steps.append(
                B7BStepResult(
                    step="runner_failure",
                    ok=False,
                    code="B7B_RUN_FAILED",
                    message=str(exc),
                    details={"checkpoint": checkpoint},
                )
            )
            return self._finalize(report_steps, summary, report_path)

    def _run_rollback_drill(self) -> B7BStepResult:
        service = PatchApplicationService(self.workspace_root)
        checkpoint = service.create_checkpoint()
        try:
            snapshot = WorkspaceSnapshotBuilder(
                workspace_root=self.workspace_root,
                allowed_files=["calculator.py"],
                allowed_test_commands=["python -m pytest test_calculator.py"],
                forbidden_actions=["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
                temporary_project=True,
                project_id=9300,
                task_packet={
                    "task_id": 9302,
                    "project_id": 9300,
                    "task_type": "SANDBOX_CODE_PATCH",
                    "temporary_project": True,
                    "allowed_files": ["calculator.py"],
                    "allowed_test_commands": ["python -m pytest test_calculator.py"],
                    "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
                    "max_files_changed": 1,
                    "max_patch_bytes": 1024,
                    "evidence_required": True,
                    "worker_id": "b7b-worker",
                    "approval_token": "one-time",
                    "mode": "mock",
                    "sandbox_root": str(self.workspace_root),
                    "control_plane_url": "http://127.0.0.1:8000",
                },
            ).build()
            proposal = _BrokenProvider().generate_patch(snapshot.task_packet, snapshot)
            service.validate_proposal(proposal, snapshot)
            service.apply_patch_proposal(proposal)
            test_result = service.run_allowed_test("python -m pytest test_calculator.py")
            if test_result["ok"]:
                raise PatchApplicationError("rollback drill expected test failure")
            raise PatchApplicationError("rollback drill test failed as expected")
        except Exception as exc:
            service.rollback(checkpoint)
            restored = (self.workspace_root / "calculator.py").read_text(encoding="utf-8").endswith("return a - b\n")
            return B7BStepResult(
                step="rollback_drill",
                ok=restored,
                code="ROLLBACK_VERIFIED" if restored else "ROLLBACK_FAILED",
                message="rollback restored baseline after failed patch drill" if restored else "rollback did not restore baseline",
                details={"error": str(exc), "restored": restored},
            )

    def _run_unauthorized_verification(self) -> B7BStepResult:
        service = PatchApplicationService(self.workspace_root)
        snapshot = WorkspaceSnapshotBuilder(
            workspace_root=self.workspace_root,
            allowed_files=["calculator.py"],
            allowed_test_commands=["python -m pytest test_calculator.py"],
            forbidden_actions=["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
            temporary_project=True,
            project_id=9300,
            task_packet={
                "task_id": 9303,
                "project_id": 9300,
                "task_type": "SANDBOX_CODE_PATCH",
                "temporary_project": True,
                "allowed_files": ["calculator.py"],
                "allowed_test_commands": ["python -m pytest test_calculator.py"],
                "forbidden_actions": ["shell", "delete", "payment", "browser", "crawl", "production_db_write"],
                "max_files_changed": 1,
                "max_patch_bytes": 1024,
                "evidence_required": True,
                "worker_id": "b7b-worker",
                "approval_token": "one-time",
                "mode": "mock",
                "sandbox_root": str(self.workspace_root),
                "control_plane_url": "http://127.0.0.1:8000",
            },
        ).build()
        proposal = _UnauthorizedProvider().generate_patch(snapshot.task_packet, snapshot)
        review = self.reviewer.review(proposal, ["calculator.py"])
        before = (self.workspace_root / "calculator.py").read_text(encoding="utf-8")
        if review.ok:
            return B7BStepResult(
                step="unauthorized_verification",
                ok=False,
                code="UNAUTHORIZED_BYPASS",
                message="unauthorized proposal unexpectedly approved",
                details={"proposal_id": proposal.proposal_id},
            )
        after = (self.workspace_root / "calculator.py").read_text(encoding="utf-8")
        return B7BStepResult(
            step="unauthorized_verification",
            ok=before == after,
            code=review.code,
            message=review.message,
            details={"proposal_id": proposal.proposal_id, "unchanged": before == after, "review": review.details},
        )

    def _finalize(
        self,
        report_steps: List[B7BStepResult],
        summary: Dict[str, Any],
        report_path: Optional[Path],
    ) -> B7BReport:
        report = B7BReport(
            report_id=f"b7b-{uuid.uuid4().hex[:12]}",
            created_at=utc_now(),
            repo_root=str(self.workspace_root),
            steps=report_steps,
            summary=summary,
        )
        if report_path is not None:
            report.write_to(report_path)
        return report
