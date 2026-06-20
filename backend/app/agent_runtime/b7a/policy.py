"""Task execution policy for B7A."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


DENIED = "TASK_EXECUTION_POLICY_DENIED"
ALLOWED = "ALLOWED"


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    code: str
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskExecutionPolicy:
    task_type: Optional[str]
    temporary_project: bool
    project_id: int
    sandbox_root: str
    allowed_files: List[str]
    forbidden_actions: List[str]
    allowed_test_commands: List[str]
    max_files_changed: int
    max_patch_bytes: int
    evidence_required: bool
    approval_token: Optional[str] = None
    approval_record: Optional[Dict[str, Any]] = None
    mode: str = "mock"
    control_plane_url: str = "http://127.0.0.1:8000"

    def evaluate(self) -> PolicyDecision:
        if self.mode == "live":
            return self._deny("live mode is not authorized")
        if not self._is_localhost_url(self.control_plane_url):
            return self._deny("control_plane_url must be localhost")
        if self.project_id in (56, 118):
            return self._deny("project is blocked")
        if not self.task_type:
            return self._deny("task_type is required")
        if self.task_type != "SANDBOX_CODE_PATCH":
            return self._deny("unsupported task_type")
        if not self.temporary_project:
            return self._deny("temporary_project must be true")
        if not self._is_safe_temp_root(self.sandbox_root):
            return self._deny("sandbox_root must be a temporary directory")
        if not self.allowed_files:
            return self._deny("allowed_files must not be empty")
        if not self.forbidden_actions:
            return self._deny("forbidden_actions must not be empty")
        if not self.allowed_test_commands:
            return self._deny("allowed_test_commands must not be empty")
        if self.max_files_changed <= 0:
            return self._deny("max_files_changed must be positive")
        if self.max_patch_bytes <= 0:
            return self._deny("max_patch_bytes must be positive")
        if not self.evidence_required:
            return self._deny("evidence_required must be true")
        if not (self.approval_token or self.approval_record):
            return self._deny("approval is required")
        if any(action in ("shell", "cmd", "delete", "payment", "browser", "crawl", "production_db_write") for action in self.forbidden_actions):
            return PolicyDecision(True, ALLOWED, "policy accepted", {"mode": self.mode})
        return PolicyDecision(True, ALLOWED, "policy accepted", {"mode": self.mode})

    def _deny(self, reason: str) -> PolicyDecision:
        return PolicyDecision(False, DENIED, reason)

    def _is_localhost_url(self, value: str) -> bool:
        parsed = urlparse(str(value or "").strip())
        return parsed.scheme == "http" and (parsed.hostname in {"127.0.0.1", "localhost", "::1"})

    def _is_safe_temp_root(self, value: str) -> bool:
        low = str(value or "").lower()
        return any(token in low for token in ("temp", "tmp", "temporary"))
