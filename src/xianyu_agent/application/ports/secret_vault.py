"""Application contract for storing secrets behind opaque references."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Opaque, persistable reference to a secret held by a vault adapter."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("secret reference must not be empty")


class SecretValue:
    """Short-lived plaintext secret that never reveals itself through repr/str."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("secret value must not be empty")
        self._value = value

    def reveal(self) -> str:
        """Reveal plaintext only at the controlled infrastructure boundary."""
        return self._value

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    """Non-sensitive metadata associated with a secret reference."""

    values: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized = {str(key): str(value) for key, value in self.values.items()}
        object.__setattr__(self, "values", MappingProxyType(normalized))


@runtime_checkable
class SecretVault(Protocol):
    """Vault port used by domain/application code without exposing plaintext state."""

    async def put(
        self,
        secret: SecretValue,
        *,
        metadata: SecretMetadata | None = None,
    ) -> SecretRef: ...

    async def replace(self, ref: SecretRef, secret: SecretValue) -> SecretRef: ...

    async def resolve(self, ref: SecretRef) -> SecretValue: ...

    async def delete(self, ref: SecretRef) -> None: ...

    async def configured(self, ref: SecretRef) -> bool: ...
