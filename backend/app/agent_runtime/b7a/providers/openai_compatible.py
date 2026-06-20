"""Disabled OpenAI-compatible provider shim for B7A."""

from __future__ import annotations

from typing import Any, Dict

from ..models import PatchProposal, WorkspaceSnapshot
from .base import PatchProvider


class OpenAICompatibleProvider(PatchProvider):
    provider_name = "openai-compatible"

    def __init__(self, *, enabled: bool = False, secret_configured: bool = False):
        self.enabled = enabled
        self.secret_configured = secret_configured

    def validate_config(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "error_code": "PROVIDER_DISABLED", "message": "OpenAI-compatible provider is disabled"}
        if not self.secret_configured:
            return {"ok": False, "error_code": "SECRET_NOT_CONFIGURED", "message": "secret is required"}
        return {"ok": True, "provider": self.provider_name}

    def health_check(self) -> Dict[str, Any]:
        return {"ok": bool(self.enabled and self.secret_configured), "provider": self.provider_name, "redacted": {"configured": self.secret_configured}}

    def generate_patch(self, task_packet: Dict[str, Any], workspace_snapshot: WorkspaceSnapshot) -> PatchProposal:
        raise RuntimeError("OpenAI-compatible provider is disabled in this runtime")

    def repair_patch(self, proposal: PatchProposal, reason: str) -> PatchProposal:
        raise RuntimeError("OpenAI-compatible provider is disabled in this runtime")

    def redact_config(self) -> Dict[str, Any]:
        return {"provider": self.provider_name, "configured": bool(self.enabled and self.secret_configured)}
