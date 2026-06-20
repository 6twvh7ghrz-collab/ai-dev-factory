"""Structured models for B7A patch proposals and workspace snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import difflib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class PatchFile:
    relative_path: str
    operation: str
    expected_sha256: str
    new_content: Optional[str] = None
    diff: Optional[str] = None
    encoding: str = "utf-8"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PatchProposal:
    proposal_id: str
    task_id: int
    provider: str
    files: List[PatchFile]
    unified_diff: str
    explanation: str
    expected_tests: List[str]
    risks: List[str]
    generated_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["files"] = [item.as_dict() for item in self.files]
        return payload


@dataclass(slots=True)
class WorkspaceSnapshot:
    task_packet: Dict[str, Any]
    allowed_files: Dict[str, str]
    file_hashes: Dict[str, str]
    directory_listing: List[str]
    allowed_test_commands: List[str]
    forbidden_actions: List[str]
    temporary_project: bool
    project_id: int
    workspace_root: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def safe_task_packet(self) -> Dict[str, Any]:
        return sanitize_task_packet_for_provider(self.task_packet)


@dataclass(slots=True)
class EvidenceBundle:
    evidence_id: str
    files_changed: List[str]
    tests_run: List[Dict[str, Any]]
    artifacts: List[Dict[str, Any]]
    summary: str
    generated_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


_SENSITIVE_KEY_FRAGMENTS = {
    "api_key",
    "access_token",
    "refresh_token",
    "lease_token",
    "approval_token",
    "authorization",
    "database_url",
    "password",
    "secret",
    "cookie",
    "credential",
    "token",
}


def sanitize_task_packet_for_provider(packet: Any) -> Any:
    if isinstance(packet, dict):
        safe: Dict[str, Any] = {}
        for key, value in packet.items():
            if _is_sensitive_key(key):
                continue
            safe[key] = sanitize_task_packet_for_provider(value)
        return safe
    if isinstance(packet, list):
        return [sanitize_task_packet_for_provider(item) for item in packet]
    if isinstance(packet, tuple):
        return [sanitize_task_packet_for_provider(item) for item in packet]
    return packet


def build_unified_diff(
    relative_path: str,
    original_content: str,
    new_content: str,
) -> str:
    before = original_content.splitlines(keepends=True)
    after = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        before,
        after,
        fromfile=f"a/{relative_path}",
        tofile=f"b/{relative_path}",
        lineterm="",
    )
    return "\n".join(diff)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lower = key.casefold()
    return any(fragment in lower for fragment in _SENSITIVE_KEY_FRAGMENTS)
