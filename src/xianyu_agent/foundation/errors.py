"""Stable machine-readable error contract."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from .identifiers import CorrelationId


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    NEEDS_VALIDATION = "NEEDS_VALIDATION"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    BLOCKED = "BLOCKED"
    RETRYABLE = "RETRYABLE"
    UNCERTAIN = "UNCERTAIN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class MachineError(Exception):
    """Transport-neutral error whose serialized message/details must be safe."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        correlation_id: CorrelationId,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        safe_message = message.strip()
        if not safe_message:
            raise ValueError("error message must not be blank")
        super().__init__(safe_message)
        self.code = code
        self.message = safe_message
        self.correlation_id = correlation_id
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "correlation_id": self.correlation_id.value,
                "details": dict(self.details),
            }
        }
