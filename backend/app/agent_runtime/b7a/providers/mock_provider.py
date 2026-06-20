"""Deterministic mock provider for B7A."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List

from ..models import PatchFile, PatchProposal, WorkspaceSnapshot, utc_now
from .base import PatchProvider


class MockProvider(PatchProvider):
    provider_name = "mock"

    def __init__(self, *, allowed_files: List[str] | None = None):
        self.allowed_files = allowed_files or []

    def validate_config(self) -> Dict[str, Any]:
        return {"ok": True, "enabled": True, "provider": self.provider_name}

    def health_check(self) -> Dict[str, Any]:
        return {"ok": True, "provider": self.provider_name, "redacted": self.redact_config()}

    def generate_patch(self, task_packet: Dict[str, Any], workspace_snapshot: WorkspaceSnapshot) -> PatchProposal:
        files = []
        unified_diff = ""
        for rel_path, content in workspace_snapshot.allowed_files.items():
            if rel_path.endswith("calculator.py") and "return a - b" in content:
                new_content = content.replace("return a - b", "return a + b")
                files.append(
                    PatchFile(
                        relative_path=rel_path,
                        operation="modify",
                        expected_sha256=workspace_snapshot.file_hashes[rel_path],
                        new_content=new_content,
                        encoding="utf-8",
                    )
                )
                unified_diff = f"--- a/{rel_path}\n+++ b/{rel_path}\n@@\n-    return a - b\n+    return a + b\n"
                break
        if not files and workspace_snapshot.allowed_files:
            rel_path, content = next(iter(workspace_snapshot.allowed_files.items()))
            files.append(
                PatchFile(
                    relative_path=rel_path,
                    operation="modify",
                    expected_sha256=workspace_snapshot.file_hashes[rel_path],
                    new_content=content,
                    encoding="utf-8",
                )
            )
        proposal_id = hashlib.sha256(f"{task_packet.get('task_id')}:{utc_now()}:{self.provider_name}".encode()).hexdigest()[:16]
        return PatchProposal(
            proposal_id=f"pp-{proposal_id}",
            task_id=int(task_packet.get("task_id", 0)),
            provider=self.provider_name,
            files=files,
            unified_diff=unified_diff,
            explanation="deterministic mock patch proposal",
            expected_tests=[cmd for cmd in workspace_snapshot.allowed_test_commands[:1]],
            risks=["mock provider returns deterministic patch"],
            generated_at=utc_now(),
            metadata={"task_type": task_packet.get("task_type", "")},
        )

    def repair_patch(self, proposal: PatchProposal, reason: str) -> PatchProposal:
        proposal.metadata = dict(proposal.metadata)
        proposal.metadata["repair_reason"] = reason
        proposal.explanation = f"{proposal.explanation} | repair: {reason}"
        return proposal

    def redact_config(self) -> Dict[str, Any]:
        return {"provider": self.provider_name, "configured": True}
