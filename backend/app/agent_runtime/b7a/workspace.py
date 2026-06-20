"""Workspace snapshot helpers for B7A."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

from .models import WorkspaceSnapshot


def _sha256_text(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class WorkspaceSnapshotBuilder:
    workspace_root: Path
    allowed_files: List[str]
    allowed_test_commands: List[str]
    forbidden_actions: List[str]
    temporary_project: bool
    project_id: int
    task_packet: Dict[str, object]

    def build(self) -> WorkspaceSnapshot:
        root = self.workspace_root.resolve(strict=False)
        files: Dict[str, str] = {}
        hashes: Dict[str, str] = {}
        listing = []
        for rel in self.allowed_files:
            candidate = (root / rel).resolve(strict=False)
            candidate.relative_to(root)
            if not candidate.exists() or not candidate.is_file():
                continue
            data = candidate.read_text(encoding="utf-8")
            files[rel] = data
            hashes[rel] = _sha256_text(data)
            listing.append(rel)
        return WorkspaceSnapshot(
            task_packet=dict(self.task_packet),
            allowed_files=files,
            file_hashes=hashes,
            directory_listing=listing,
            allowed_test_commands=list(self.allowed_test_commands),
            forbidden_actions=list(self.forbidden_actions),
            temporary_project=self.temporary_project,
            project_id=self.project_id,
            workspace_root=str(root),
            metadata={"allowed_files": list(self.allowed_files)},
        )
