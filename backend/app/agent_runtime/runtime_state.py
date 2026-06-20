"""State model for the persistent local agent runtime."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


STOPPED = "STOPPED"
STARTING = "STARTING"
IDLE = "IDLE"
CLAIMING = "CLAIMING"
RUNNING_PROBE = "RUNNING_PROBE"
SUBMITTING = "SUBMITTING"
BACKING_OFF = "BACKING_OFF"
STOPPING = "STOPPING"
FAILED = "FAILED"


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@dataclass
class RuntimeState:
    runtime_status: str = STOPPED
    pid: int = 0
    worker_id: str = ""
    mode: str = "mock"
    control_plane_url: str = ""
    current_task_id: Optional[int] = None
    current_assignment_id: Optional[str] = None
    heartbeat_active: bool = False
    last_cycle_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error_code: Optional[str] = None
    cycles_completed: int = 0
    started_at: Optional[str] = None

    def safe_dict(self) -> dict:
        return asdict(self)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.safe_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def for_config(cls, config) -> "RuntimeState":
        return cls(
            runtime_status=STOPPED,
            pid=os.getpid(),
            worker_id=config.worker_id,
            mode=config.mode,
            control_plane_url=config.control_plane_url,
        )
