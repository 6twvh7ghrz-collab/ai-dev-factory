"""Data models for the local sandbox agent connector."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse


ALLOWED_CONTROL_PLANE_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_local_url(control_plane_url: str) -> str:
    parsed = urlparse(str(control_plane_url or "").strip())
    if parsed.scheme != "http":
        raise ValueError("control_plane_url must use http://localhost or http://127.0.0.1")
    host = parsed.hostname or ""
    if host not in ALLOWED_CONTROL_PLANE_HOSTS:
        raise ValueError("control_plane_url must target localhost only")
    if not parsed.netloc:
        raise ValueError("control_plane_url is invalid")
    return f"http://{parsed.netloc}"


def _normalize_sandbox_root(sandbox_root: str | Path) -> Path:
    path = Path(sandbox_root).expanduser()
    if not path.is_absolute():
        raise ValueError("sandbox_root must be an absolute path")
    resolved = path.resolve(strict=False)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


@dataclass(slots=True)
class ConnectorConfig:
    control_plane_url: str
    worker_id: str
    worker_type: str = "executor"
    project_id: int = 0
    allowed_task_ids: List[int] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    sandbox_root: Path = field(default_factory=Path)
    heartbeat_interval: float = 2.0
    lease_seconds: int = 300
    request_timeout: float = 10.0
    max_retries: int = 3
    dry_run: bool = True

    def __post_init__(self) -> None:
        self.control_plane_url = _normalize_local_url(self.control_plane_url)
        self.sandbox_root = _normalize_sandbox_root(self.sandbox_root)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectorConfig":
        payload = dict(data)
        payload["sandbox_root"] = payload.get("sandbox_root") or payload.get("sandboxRoot") or ""
        return cls(**payload)

    @classmethod
    def from_file(cls, path: str | Path) -> "ConnectorConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)


@dataclass(slots=True)
class ProbeArtifact:
    artifact_id: str
    artifact_type: str
    uri: str
    sha256: str
    size_bytes: int
    mime_type: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ProbeExecution:
    task_id: int
    worker_id: str
    execution_id: str
    file_path: Path
    content: str
    sha256: str
    artifact: ProbeArtifact

    @property
    def result_packet(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "result_status": "submitted",
            "files_modified": [self.file_path.name],
            "files_checked": [self.file_path.name],
            "diff_summary": "probe_result.txt created",
            "tests": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "output": "probe passed"},
            "git_commit": "0123456789abcdef0123456789abcdef01234567",
            "git_branch": "sandbox/probe",
            "base_commit": "89abcdef0123456789abcdef0123456789abcdef",
            "exit_code": 0,
            "stdout": self.content,
            "stderr": "",
            "manual_actions": [{"action": "created probe_result.txt", "actor": "connector"}],
            "errors": [],
            "evidence_refs": [self.artifact.artifact_id],
            "artifacts": [self.artifact.as_dict()],
            "handoff_requested": False,
            "remaining_steps": [],
            "worker_id": self.worker_id,
            "submitted_at": "2026-06-20 00:00:00",
            "duration_ms": 10,
            "model_calls": 0,
            "repair_attempts": 0,
        }

