"""Deterministic probe task executor for the sandbox connector."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict
from urllib.parse import unquote

from .models import ProbeArtifact, ProbeExecution


MAX_SAFE_PATH_LENGTH = 512


def _safe_relative_path(value: str) -> Path:
    raw = unquote(str(value or "")).replace("\\", "/").strip()
    if not raw or len(raw) > MAX_SAFE_PATH_LENGTH:
        raise ValueError("path is invalid")
    if raw.startswith("/") or raw.startswith("~") or "://" in raw:
        raise ValueError("path is invalid")
    parts = [part for part in raw.split("/") if part]
    if ".." in parts:
        raise ValueError("path is invalid")
    if len(parts) == 1:
        return Path(parts[0])
    return Path(*parts)


def _resolve_inside_root(root: Path, relative_path: str) -> Path:
    root_resolved = root.resolve(strict=False)
    candidate = root_resolved / _safe_relative_path(relative_path)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("path escapes sandbox_root") from exc
    if candidate_resolved.is_symlink():
        raise ValueError("path escapes sandbox_root")
    return candidate_resolved


def execute_probe_task(*, sandbox_root: Path, task_id: int, worker_id: str, execution_id: str = "") -> ProbeExecution:
    sandbox_root = Path(sandbox_root).resolve(strict=False)
    sandbox_root.mkdir(parents=True, exist_ok=True)
    probe_path = _resolve_inside_root(sandbox_root, "probe_result.txt")
    content = f"probe-result task={task_id} worker={worker_id}\n"
    probe_path.write_text(content, encoding="utf-8")
    raw = probe_path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    artifact = ProbeArtifact(
        artifact_id=f"artifact-{task_id}-{execution_id or worker_id}",
        artifact_type="test_report",
        uri="probe_result.txt",
        sha256=sha256,
        size_bytes=len(raw),
        mime_type="text/plain",
        metadata={"probe": True, "task_id": task_id, "worker_id": worker_id},
    )
    return ProbeExecution(
        task_id=task_id,
        worker_id=worker_id,
        execution_id=execution_id or f"exec-{task_id}",
        file_path=probe_path,
        content=content,
        sha256=sha256,
        artifact=artifact,
    )


