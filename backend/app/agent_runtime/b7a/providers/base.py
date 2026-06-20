"""Base provider interfaces for B7A."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from ..models import PatchProposal, WorkspaceSnapshot


class PatchProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    def validate_config(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def generate_patch(self, task_packet: Dict[str, Any], workspace_snapshot: WorkspaceSnapshot) -> PatchProposal:
        raise NotImplementedError

    @abstractmethod
    def repair_patch(self, proposal: PatchProposal, reason: str) -> PatchProposal:
        raise NotImplementedError

    @abstractmethod
    def redact_config(self) -> Dict[str, Any]:
        raise NotImplementedError
