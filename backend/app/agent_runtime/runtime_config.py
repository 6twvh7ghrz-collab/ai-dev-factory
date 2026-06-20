"""Configuration for the persistent local agent runtime."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from .models import _normalize_local_url


RUNTIME_MODES = {"mock", "sandbox", "live"}


def runtime_dir() -> Path:
    root = Path(os.getenv("AI_FACTORY_RUNTIME_DIR") or Path(tempfile.gettempdir()) / "ai-dev-factory-public" / "agent-runtime")
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass(slots=True)
class RuntimeConfig:
    mode: str = "mock"
    control_plane_url: str = "http://127.0.0.1:8000"
    worker_id: str = "local-agent-runtime"
    project_id: int = 0
    allowed_task_ids: List[int] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=lambda: ["control_plane_probe", "probe", "python"])
    sandbox_root: Path = field(default_factory=lambda: runtime_dir() / "sandbox")
    heartbeat_interval: float = 2.0
    lease_seconds: int = 300
    poll_interval: float = 1.0
    request_timeout: float = 10.0
    max_retries: int = 3
    dry_run: bool = True
    runtime_dir: Path = field(default_factory=runtime_dir)
    max_cycles: int = 0

    def __post_init__(self) -> None:
        self.mode = str(self.mode or "mock").lower()
        if self.mode not in RUNTIME_MODES:
            raise ValueError("mode must be mock, sandbox, or live")
        self.control_plane_url = _normalize_local_url(self.control_plane_url)
        self.sandbox_root = Path(self.sandbox_root).expanduser().resolve(strict=False)
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = Path(self.runtime_dir).expanduser().resolve(strict=False)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_task_ids = [int(v) for v in self.allowed_task_ids]
        self.capabilities = [str(v) for v in self.capabilities]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeConfig":
        payload = dict(data)
        payload["sandbox_root"] = payload.get("sandbox_root") or payload.get("sandboxRoot") or runtime_dir() / "sandbox"
        payload["runtime_dir"] = payload.get("runtime_dir") or payload.get("runtimeDir") or runtime_dir()
        return cls(**payload)

    @classmethod
    def from_file(cls, path: str | Path) -> "RuntimeConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_connector_config(self):
        from .models import ConnectorConfig

        return ConnectorConfig(
            control_plane_url=self.control_plane_url,
            worker_id=self.worker_id,
            worker_type="executor",
            project_id=self.project_id,
            allowed_task_ids=list(self.allowed_task_ids),
            capabilities=list(self.capabilities),
            sandbox_root=self.sandbox_root,
            heartbeat_interval=self.heartbeat_interval,
            lease_seconds=self.lease_seconds,
            request_timeout=self.request_timeout,
            max_retries=self.max_retries,
            dry_run=self.dry_run,
        )

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / "agent-runtime.pid"

    @property
    def lock_file(self) -> Path:
        return self.runtime_dir / "agent-runtime.lock"

    @property
    def stop_file(self) -> Path:
        return self.runtime_dir / "agent-runtime.stop"

    @property
    def status_file(self) -> Path:
        return self.runtime_dir / "agent-runtime.status.json"

    def real_writes_allowed(self) -> bool:
        control = os.getenv("V2_CONTROL_PLANE_ENABLED", "false").lower() in ("true", "1")
        runtime = os.getenv("V2_AGENT_RUNTIME_ENABLED", "false").lower() in ("true", "1")
        return control and runtime
