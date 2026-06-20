"""Controlled patch application and rollback for B7A."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import EvidenceBundle, PatchFile, PatchProposal, WorkspaceSnapshot, utc_now
from .policy import TaskExecutionPolicy


class PatchApplicationError(RuntimeError):
    pass


@dataclass(slots=True)
class PatchApplicationService:
    workspace_root: Path
    commit_changes: bool = False

    def verify_workspace(self) -> None:
        if not (self.workspace_root / ".git").exists():
            raise PatchApplicationError("workspace is not a git repository")
        if self._git_status_porcelain():
            raise PatchApplicationError("workspace must be clean")

    def create_checkpoint(self) -> str:
        return self._git_rev_parse("HEAD")

    def validate_proposal(
        self,
        proposal: PatchProposal,
        snapshot: WorkspaceSnapshot,
        policy_decision: Optional[TaskExecutionPolicy] = None,
    ) -> None:
        if proposal.provider not in {"mock", "openai-compatible", "codex"}:
            raise PatchApplicationError("unsupported provider")
        if not proposal.files:
            raise PatchApplicationError("proposal must contain files")
        if len(proposal.files) > int(snapshot.task_packet.get("max_files_changed", len(proposal.files))):
            raise PatchApplicationError("file count exceeds policy")

        max_patch_bytes = int(snapshot.task_packet.get("max_patch_bytes", 0) or 0)
        total_bytes = 0
        allowed = set(snapshot.allowed_files.keys())
        for item in proposal.files:
            self._validate_path(item.relative_path)
            if item.relative_path not in allowed:
                raise PatchApplicationError("file is outside allowed scope")
            if self._is_forbidden_target(item.relative_path):
                raise PatchApplicationError("file is not allowed")
            if item.expected_sha256 != snapshot.file_hashes.get(item.relative_path):
                raise PatchApplicationError("file hash mismatch")
            if item.new_content is not None:
                encoded = item.new_content.encode(item.encoding)
                total_bytes += len(encoded)
                try:
                    encoded.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise PatchApplicationError("content must be valid utf-8") from exc
            elif item.diff is not None:
                total_bytes += len(item.diff.encode("utf-8"))
        if max_patch_bytes and total_bytes > max_patch_bytes:
            raise PatchApplicationError("patch exceeds size limit")
        if ".." in json.dumps([item.relative_path for item in proposal.files]):
            raise PatchApplicationError("path traversal rejected")

    def apply_patch_proposal(self, proposal: PatchProposal) -> List[str]:
        changed = []
        for item in proposal.files:
            target = (self.workspace_root / item.relative_path).resolve(strict=False)
            target.relative_to(self.workspace_root.resolve(strict=False))
            if item.operation not in {"modify", "add"}:
                raise PatchApplicationError(f"unsupported operation: {item.operation}")
            if item.new_content is None:
                raise PatchApplicationError("new_content is required")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.new_content, encoding=item.encoding)
            changed.append(item.relative_path)
        return changed

    def run_allowed_test(self, command: str, timeout_seconds: int = 120) -> Dict[str, Any]:
        argv = self._normalize_command(command)
        proc = subprocess.run(
            argv,
            cwd=str(self.workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        return {
            "command": command,
            "argv": argv,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "ok": proc.returncode == 0,
        }

    def rollback(self, checkpoint: str) -> None:
        self._git(["reset", "--hard", checkpoint])
        self._git(["clean", "-fd"])

    def finalize_evidence(
        self,
        *,
        proposal: PatchProposal,
        changed_files: List[str],
        tests_run: List[Dict[str, Any]],
        summary: str,
    ) -> EvidenceBundle:
        artifacts = []
        for rel in changed_files:
            path = self.workspace_root / rel
            artifacts.append(
                {
                    "artifact_id": f"artifact-{rel.replace('/', '-')}",
                    "artifact_type": "document",
                    "uri": rel,
                    "sha256": self._sha256(path.read_bytes()),
                    "size_bytes": path.stat().st_size,
                    "mime_type": "text/plain",
                    "metadata": {"proposal_id": proposal.proposal_id},
                }
            )
        return EvidenceBundle(
            evidence_id=f"evidence-{proposal.proposal_id}",
            files_changed=list(changed_files),
            tests_run=list(tests_run),
            artifacts=artifacts,
            summary=summary,
            generated_at=utc_now(),
            metadata={"proposal_id": proposal.proposal_id},
        )

    def _normalize_command(self, command: str) -> List[str]:
        tokens = shlex.split(str(command or ""))
        if not tokens:
            raise PatchApplicationError("test command is empty")
        if tokens[:3] == ["python", "-m", "pytest"]:
            return tokens
        if tokens == ["npm", "run", "typecheck"]:
            return tokens
        if tokens == ["npm", "test"]:
            return tokens
        if tokens == ["npm", "run", "build"]:
            return tokens
        raise PatchApplicationError("command is not in allowlist")

    def _validate_path(self, value: str) -> None:
        raw = str(value or "")
        if not raw or raw.startswith(("/", "\\\\")) or "://" in raw or ".." in Path(raw).parts:
            raise PatchApplicationError("path is invalid")
        if Path(raw).is_absolute():
            raise PatchApplicationError("absolute paths are not allowed")
        if self._is_forbidden_target(raw):
            raise PatchApplicationError("path is not allowed")

    def _git_status_porcelain(self) -> str:
        return self._git(["status", "--porcelain"])

    def _git_rev_parse(self, ref: str) -> str:
        return self._git(["rev-parse", ref]).strip()

    def _git(self, argv: List[str]) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.workspace_root), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            shell=False,
        )
        if proc.returncode != 0:
            raise PatchApplicationError(proc.stderr.strip() or "git command failed")
        return proc.stdout

    def _sha256(self, data: bytes) -> str:
        import hashlib

        return hashlib.sha256(data).hexdigest()

    def _is_forbidden_target(self, relative_path: str) -> bool:
        path = Path(relative_path)
        parts = {part.lower() for part in path.parts}
        suffix = path.suffix.lower()
        if ".git" in parts or ".env" in parts:
            return True
        if suffix in {".db", ".sqlite", ".sqlite3", ".dll", ".exe", ".bin", ".png", ".jpg", ".jpeg", ".gif", ".bmp"}:
            return True
        if path.name.lower() in {".env", ".git"}:
            return True
        return False
