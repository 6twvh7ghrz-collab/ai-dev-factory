"""Background heartbeat loop for a claimed sandbox task."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .client import ControlPlaneClient


FATAL_ERROR_CODES = {
    "VALIDATION_ERROR",
    "WORKER_NOT_REGISTERED",
    "TASK_SCOPE_VIOLATION",
    "WORKER_CAPABILITY_MISMATCH",
    "WORKER_TYPE_NOT_ALLOWED",
    "STALE_LEASE",
    "IDEMPOTENCY_CONFLICT",
}


@dataclass
class HeartbeatLoop:
    client: ControlPlaneClient
    task_id: int
    assignment_id: str
    worker_id: str
    lease_token: str
    interval: float = 2.0
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: Optional[threading.Thread] = field(default=None, init=False)
    last_error: Optional[str] = field(default=None, init=False)
    _counter: int = field(default=0, init=False)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{self.worker_id}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._counter += 1
            key = f"hb-{self.task_id}-{self.assignment_id}-{self._counter}"
            try:
                result = self.client.heartbeat(self.task_id, self.assignment_id, self.lease_token, idem_key=key)
                if not result.get("ok"):
                    code = result.get("error_code") or "INTERNAL_ERROR"
                    self.last_error = code
                    if code in FATAL_ERROR_CODES:
                        self.stop()
                        return
            except Exception as exc:  # pragma: no cover - network failures are retried elsewhere
                self.last_error = str(exc)
                time.sleep(min(self.interval, 1.0))
                continue
            self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())


