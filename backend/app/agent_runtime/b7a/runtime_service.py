"""Local runtime service for B7A sandbox code patch execution."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .bridge import B7AExecutionBridge
from .models import utc_now
from .policy import TaskExecutionPolicy
from ..runtime_state import (
    BACKING_OFF,
    FAILED,
    IDLE,
    STARTING,
    STOPPED,
    STOPPING,
    RuntimeState,
)


@dataclass(slots=True)
class B7ARuntimeService:
    bridge: B7AExecutionBridge
    runtime_dir: Path
    state: RuntimeState = field(init=False)
    _lock_handle: Optional[Any] = field(default=None, init=False)
    _stop_requested: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.runtime_dir = Path(self.runtime_dir).expanduser().resolve(strict=False)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.state = RuntimeState(
            runtime_status=STOPPED,
            pid=os.getpid(),
            worker_id="b7a-runtime",
            mode="mock",
            control_plane_url="local",
        )

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / "b7a-runtime.pid"

    @property
    def lock_file(self) -> Path:
        return self.runtime_dir / "b7a-runtime.lock"

    @property
    def status_file(self) -> Path:
        return self.runtime_dir / "b7a-runtime.status.json"

    def start(self) -> Dict[str, Any]:
        self._acquire_lock()
        self.state.runtime_status = STARTING
        self.state.started_at = utc_now()
        self.state.pid = os.getpid()
        self._write_state()
        self.state.runtime_status = IDLE
        self._write_state()
        return {"ok": True, "runtime_status": self.state.runtime_status}

    def run_one_cycle(self, *, task_packet: Dict[str, Any], workspace_root: Path, policy: TaskExecutionPolicy) -> Dict[str, Any]:
        if self.state.runtime_status == STOPPED:
            self.start()
        result = self.bridge.execute(task_packet=task_packet, workspace_root=workspace_root, policy=policy)
        self.state.last_cycle_at = utc_now()
        if result.get("ok"):
            self.state.runtime_status = IDLE
            self.state.last_success_at = utc_now()
            self.state.cycles_completed += 1
            self.state.current_task_id = None
            self.state.current_assignment_id = None
            self._write_state()
        else:
            self.state.runtime_status = FAILED
            self.state.last_error_code = result.get("error_code")
            self._write_state()
        return result

    def stop(self) -> Dict[str, Any]:
        self._stop_requested = True
        self.state.runtime_status = STOPPING
        self._write_state()
        self.shutdown()
        return {"ok": True, "runtime_status": self.state.runtime_status}

    def status(self) -> Dict[str, Any]:
        return self.state.safe_dict()

    def shutdown(self) -> None:
        if self._lock_handle:
            self._lock_handle.close()
            self._lock_handle = None
        for path in (self.pid_file, self.lock_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.state.runtime_status = STOPPED
        self._write_state()

    def _acquire_lock(self) -> None:
        if self.lock_file.exists():
            raise RuntimeError("B7A_RUNTIME_ALREADY_RUNNING")
        self._lock_handle = open(self.lock_file, "x", encoding="utf-8")
        self._lock_handle.write(str(os.getpid()))
        self._lock_handle.flush()
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")

    def _write_state(self) -> None:
        self.status_file.write_text(json.dumps(self.state.safe_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
