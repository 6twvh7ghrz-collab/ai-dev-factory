"""Runtime secret providers for B7A."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional


class RuntimeSecretProvider:
    def configured(self) -> bool:
        raise NotImplementedError

    def resolve(self, name: str) -> Optional[str]:
        raise NotImplementedError

    def status(self) -> Dict[str, bool]:
        return {"configured": self.configured()}


@dataclass(slots=True)
class MemorySecretProvider(RuntimeSecretProvider):
    secrets: Dict[str, str] = field(default_factory=dict)

    def configured(self) -> bool:
        return bool(self.secrets)

    def resolve(self, name: str) -> Optional[str]:
        return self.secrets.get(name)


@dataclass(slots=True)
class EnvSecretProvider(RuntimeSecretProvider):
    """Resolve secret references from environment variables.

    Supported references:
    - `env:NAME`
    - `NAME` when the variable exists in the environment
    """

    refs: Dict[str, str] = field(default_factory=dict)

    def configured(self) -> bool:
        return bool(self.refs)

    def resolve(self, name: str) -> Optional[str]:
        ref = self.refs.get(name)
        if ref is None:
            return None
        if ref.startswith("env:"):
            return os.getenv(ref[4:])
        if ref in os.environ:
            return os.getenv(ref)
        return None

