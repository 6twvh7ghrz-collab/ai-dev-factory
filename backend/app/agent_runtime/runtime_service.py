"""Safe persistent runtime harness for the local agent connector."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .local_agent_connector import LocalAgentConnector
from .runtime_config import RuntimeConfig
from .runtime_state import (
    BACKING_OFF,
    CLAIMING,
    FAILED,
    IDLE,
    RUNNING_PROBE,
    STARTING,
    STOPPED,
    STOPPING,
    SUBMITTING,
    RuntimeState,
    utc_now,
)


FATAL_ERROR_CODES = {
    "VALIDATION_ERROR",
    "WORKER_NOT_REGISTERED",
    "TASK_SCOPE_VIOLATION",
    "WORKER_CAPABILITY_MISMATCH",
    "WORKER_TYPE_NOT_ALLOWED",
    "STALE_LEASE",
    "IDEMPOTENCY_CONFLICT",
    "RUNTIME_LIVE_MODE_NOT_AUTHORIZED",
}


class RuntimeLockError(RuntimeError):
    pass


class MockControlPlaneClient:
    def __init__(self, config):
        self.config = config
        self.calls = []

    def register_worker(self) -> Dict[str, Any]:
        self.calls.append("register")
        return {"ok": True, "worker": {"worker_id": self.config.worker_id}}

    def claim_task(self, task_id: int, expected_version: int) -> Dict[str, Any]:
        self.calls.append("claim")
        if task_id <= 0:
            return {"ok": False, "error_code": "NO_TASK", "message": "no mock task configured"}
        return {
            "ok": True,
            "assignment_id": "mock-assignment",
            "task_id": task_id,
            "worker_id": self.config.worker_id,
            "lease_token": "mock-lease-token",
            "state_version": expected_version,
            "task_packet": {"title": "CONTROL_PLANE_PROBE"},
        }

    def heartbeat(self, task_id: int, assignment_id: str, lease_token: str, *, idem_key: str) -> Dict[str, Any]:
        self.calls.append("heartbeat")
        return {"ok": True, "heartbeat_id": idem_key, "assignment_id": assignment_id, "task_id": task_id}

    def submit_result(self, task_id: int, packet: Dict[str, Any], *, lease_token: str, assignment_id: str, expected_version: int, idem_key: str) -> Dict[str, Any]:
        self.calls.append("submit")
        return {"ok": True, "task_state": "RESULT_SUBMITTED", "assignment_id": assignment_id, "task_id": task_id}

    def close(self) -> None:
        self.calls.append("close")


class AgentRuntimeService:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        client_factory: Optional[Callable[[Any], Any]] = None,
        after_register: Optional[Callable[[LocalAgentConnector], None]] = None,
        after_claim: Optional[Callable[[LocalAgentConnector], None]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.state = RuntimeState.for_config(config)
        self.connector: Optional[LocalAgentConnector] = None
        self.client_factory = client_factory
        self.after_register = after_register
        self.after_claim = after_claim
        self.sleep_fn = sleep_fn
        self._stop_requested = False
        self._lock_handle = None

    def start(self) -> Dict[str, Any]:
        self._validate_start()
        self._acquire_lock()
        self._stop_requested = False
        self.state.runtime_status = STARTING
        self.state.pid = os.getpid()
        self.state.started_at = utc_now()
        self._write_state()
        connector_config = self.config.to_connector_config()
        self.connector = LocalAgentConnector(connector_config)
        if self.config.mode == "mock":
            self.connector.client = MockControlPlaneClient(connector_config)
        elif self.client_factory is not None:
            self.connector.client = self.client_factory(connector_config)
        register = self.connector.register()
        if not register.get("ok"):
            return self._fail(register.get("error_code") or "REGISTER_FAILED")
        if self.after_register:
            self.after_register(self.connector)
        self.state.runtime_status = IDLE
        self._write_state()
        return {"ok": True, "runtime_status": self.state.runtime_status}

    def stop(self) -> Dict[str, Any]:
        self._stop_requested = True
        self.state.runtime_status = STOPPING
        self._write_state()
        self.shutdown()
        return {"ok": True, "runtime_status": self.state.runtime_status}

    def status(self) -> Dict[str, Any]:
        self.state.heartbeat_active = bool(self.connector and self.connector.heartbeat and self.connector.heartbeat.is_running())
        return self.state.safe_dict()

    def run_one_cycle(self) -> Dict[str, Any]:
        if self.connector is None:
            result = self.start()
            if not result.get("ok"):
                return result
        if self._stop_requested or self.config.stop_file.exists():
            return self.stop()

        self.state.last_cycle_at = utc_now()
        self.state.runtime_status = CLAIMING
        self._write_state()
        if not self.config.allowed_task_ids:
            self.state.runtime_status = IDLE
            self._write_state()
            return {"ok": True, "action": "IDLE", "error_code": "NO_TASK"}
        task_id = self.config.allowed_task_ids[0]
        claim = self.connector.claim_once(task_id)
        if not claim.get("ok"):
            code = claim.get("error_code") or "NO_TASK"
            self.state.last_error_code = code
            if code in ("NO_TASK", "TASK_NOT_CLAIMABLE", "WORKER_NOT_AVAILABLE"):
                self.state.runtime_status = IDLE
                self._write_state()
                return {"ok": True, "action": "IDLE", "error_code": code}
            return self._fail(code)

        self.state.current_task_id = int(claim["task_id"])
        self.state.current_assignment_id = str(claim["assignment_id"])
        if not self._is_control_plane_probe(claim):
            return self._fail("UNSUPPORTED_TASK_TYPE")
        if self.after_claim:
            self.after_claim(self.connector)

        hb = self.connector.start_heartbeat()
        self.state.heartbeat_active = bool(hb and hb.is_running())
        self.state.runtime_status = RUNNING_PROBE
        self._write_state()
        probe = self.connector.execute_probe_task()
        if not probe.get("ok"):
            return self._fail(probe.get("error_code") or "PROBE_FAILED")

        self.state.runtime_status = SUBMITTING
        self._write_state()
        submit = self.connector.submit_result(probe)
        self.connector.stop_heartbeat()
        self.state.heartbeat_active = False
        if not submit.get("ok"):
            return self._fail(submit.get("error_code") or "SUBMIT_FAILED")

        self.state.cycles_completed += 1
        self.state.last_success_at = utc_now()
        self.state.last_error_code = None
        self.state.current_task_id = None
        self.state.current_assignment_id = None
        self.state.runtime_status = IDLE
        self._write_state()
        return {"ok": True, "action": "PROBE_SUBMITTED", "submit": submit}

    def run_forever(self) -> Dict[str, Any]:
        cycles = 0
        backoff = max(0.1, float(self.config.poll_interval))
        try:
            self.start()
            while not self._stop_requested and not self.config.stop_file.exists():
                result = self.run_one_cycle()
                cycles += 1
                if self.config.max_cycles and cycles >= self.config.max_cycles:
                    break
                if result.get("action") == "IDLE":
                    self.state.runtime_status = BACKING_OFF
                    self._write_state()
                    self.sleep_fn(backoff)
                    backoff = min(backoff * 2, 10.0)
                else:
                    backoff = max(0.1, float(self.config.poll_interval))
                if not result.get("ok") and result.get("error_code") in FATAL_ERROR_CODES:
                    break
            return {"ok": True, "cycles": cycles, "status": self.state.runtime_status}
        except KeyboardInterrupt:
            return self.stop()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self.connector:
            self.connector.shutdown()
            self.connector = None
        self.state.heartbeat_active = False
        if self.state.runtime_status not in (FAILED,):
            self.state.runtime_status = STOPPED
        self.state.current_task_id = None
        self.state.current_assignment_id = None
        self._write_state()
        self._release_lock()
        for path in (self.config.pid_file, self.config.stop_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _validate_start(self) -> None:
        if self.config.mode == "live":
            raise RuntimeError("RUNTIME_LIVE_MODE_NOT_AUTHORIZED")
        if self.config.mode == "sandbox" and not self.config.real_writes_allowed():
            raise RuntimeError("RUNTIME_DISABLED")

    def _is_control_plane_probe(self, claim: Dict[str, Any]) -> bool:
        if self.config.mode == "mock":
            return True
        packet = claim.get("task_packet") or {}
        title = str(packet.get("title") or packet.get("name") or "")
        task_type = str(packet.get("task_type") or packet.get("type") or "")
        return "CONTROL_PLANE_PROBE" in title.upper() or task_type.upper() == "CONTROL_PLANE_PROBE"

    def _fail(self, code: str) -> Dict[str, Any]:
        self.state.runtime_status = FAILED
        self.state.last_error_code = code
        self._write_state()
        return {"ok": False, "error_code": code, "runtime_status": FAILED}

    def _write_state(self) -> None:
        self.state.write(self.config.status_file)

    def _acquire_lock(self) -> None:
        try:
            self._lock_handle = open(self.config.lock_file, "x", encoding="utf-8")
            self._lock_handle.write(str(os.getpid()))
            self._lock_handle.flush()
            self.config.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except FileExistsError as exc:
            raise RuntimeLockError("RUNTIME_ALREADY_RUNNING") from exc

    def _release_lock(self) -> None:
        if self._lock_handle:
            self._lock_handle.close()
            self._lock_handle = None
        try:
            self.config.lock_file.unlink()
        except FileNotFoundError:
            pass
