"""HTTP client used by the local sandbox agent connector."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from .models import ConnectorConfig


RETRYABLE_STATUS = {500, 502, 503, 504}
RETRYABLE_ERRORS = (requests.Timeout, requests.ConnectionError)


@dataclass
class ControlPlaneClient:
    config: ConnectorConfig

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()

    def _request_once(self, method: str, path: str, *, json_body: Optional[dict], headers: Dict[str, str]) -> requests.Response:
        return self.session.request(
            method,
            self.config.control_plane_url.rstrip("/") + path,
            json=json_body,
            headers=headers,
            timeout=self.config.request_timeout,
        )

    def _request_json(self, method: str, path: str, *, json_body: Optional[dict] = None, idem_key: str = "") -> Dict[str, Any]:
        headers = {}
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        attempts = max(0, int(self.config.max_retries))
        last_error: Optional[Exception] = None
        last_response: Optional[requests.Response] = None
        for attempt in range(attempts + 1):
            try:
                resp = self._request_once(method, path, json_body=json_body, headers=headers)
                if resp.status_code in RETRYABLE_STATUS and attempt < attempts:
                    time.sleep(min(0.5 * (attempt + 1), 2.0))
                    continue
                last_response = resp
                break
            except RETRYABLE_ERRORS as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                time.sleep(min(0.5 * (attempt + 1), 2.0))
        if last_response is None:
            raise last_error or RuntimeError("request failed")
        try:
            body = last_response.json()
        except json.JSONDecodeError:
            body = {"ok": False, "error_code": "INTERNAL_ERROR", "message": last_response.text}
        body["_http_status"] = last_response.status_code
        return body

    def register_worker(self) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/v2/workers/register",
            json_body={
                "worker_id": self.config.worker_id,
                "worker_type": self.config.worker_type,
                "provider": "local",
                "display_name": self.config.worker_id,
                "capabilities": list(self.config.capabilities),
                "sandbox_profile_id": "sandbox-default",
                "metadata": {},
            },
            idem_key=f"register-{self.config.worker_id}",
        )

    def claim_task(self, task_id: int, expected_version: int) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            f"/api/v2/tasks/{task_id}/claim",
            json_body={
                "worker_id": self.config.worker_id,
                "expected_version": expected_version,
                "lease_seconds": self.config.lease_seconds,
                "allowed_task_ids": list(self.config.allowed_task_ids),
                "project_id": self.config.project_id,
            },
            idem_key=f"claim-{task_id}-{self.config.worker_id}",
        )

    def heartbeat(self, task_id: int, assignment_id: str, lease_token: str, *, idem_key: str) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            f"/api/v2/tasks/{task_id}/heartbeat",
            json_body={
                "assignment_id": assignment_id,
                "worker_id": self.config.worker_id,
                "lease_token": lease_token,
                "extend_seconds": self.config.lease_seconds,
            },
            idem_key=idem_key,
        )

    def submit_result(self, task_id: int, packet: Dict[str, Any], *, lease_token: str, assignment_id: str, expected_version: int, idem_key: str) -> Dict[str, Any]:
        payload = dict(packet)
        payload.update(
            {
                "assignment_id": assignment_id,
                "worker_id": self.config.worker_id,
                "lease_token": lease_token,
                "expected_version": expected_version,
            }
        )
        return self._request_json("POST", f"/api/v2/tasks/{task_id}/submit", json_body=payload, idem_key=idem_key)

    def request_handoff(self, task_id: int, packet: Dict[str, Any], *, idem_key: str) -> Dict[str, Any]:
        payload = dict(packet)
        payload["action"] = "request"
        return self._request_json("POST", f"/api/v2/tasks/{task_id}/handoff", json_body=payload, idem_key=idem_key)


