"""Strongly typed request/correlation identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class _OpaqueId:
    value: str

    def __post_init__(self) -> None:
        normalized = self.value.strip()
        if not normalized:
            raise ValueError(f"{type(self).__name__} must not be empty")
        object.__setattr__(self, "value", normalized)

    @classmethod
    def new(cls) -> Self:
        return cls(uuid4().hex)

    def __str__(self) -> str:
        return self.value


class RequestId(_OpaqueId):
    """Unique identifier for one ingress request/command attempt."""


class CorrelationId(_OpaqueId):
    """Identifier propagated across one logical operation."""


class CausationId(_OpaqueId):
    """Identifier of the request/event that directly caused current work."""


class IdempotencyKey(_OpaqueId):
    """Caller- or system-supplied key used to converge repeated commands."""
