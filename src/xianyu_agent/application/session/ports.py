"""Ports consumed by the credential application boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

CredentialT = TypeVar("CredentialT")


class CredentialFailureCode(StrEnum):
    """Stable machine-readable credential failure codes."""

    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    IDENTITY_MISSING = "IDENTITY_MISSING"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    NEEDS_VALIDATION = "NEEDS_VALIDATION"
    NETWORK_ERROR = "NETWORK_ERROR"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    AUTH_FAILED = "AUTH_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class CredentialBackendError(RuntimeError):
    """Sanitized backend failure crossing into the application layer."""

    def __init__(
        self,
        code: CredentialFailureCode,
        *,
        retryable_hint: bool = False,
    ) -> None:
        self.code = code
        self.retryable_hint = retryable_hint
        super().__init__(code.value)


@dataclass(frozen=True)
class CredentialBackendStatus:
    """Secret-free status reported by a protocol/infrastructure adapter."""

    account_id: str
    cookie_available: bool
    identity_available: bool
    token_cached: bool
    expires_at: datetime | None


@dataclass(frozen=True)
class ValidationStatus:
    """Persistent validation gate state, independent of WorkerState."""

    account_id: str
    required: bool
    cooling: bool = False
    code: str | None = None


class CredentialBackend(Protocol[CredentialT]):
    """Credential implementation port.

    ``acquire`` may return secret-bearing material. Callers must keep that value inside
    the application result handle instead of logging or serializing it.
    """

    async def inspect(self, account_id: str) -> CredentialBackendStatus: ...

    async def acquire(
        self,
        account_id: str,
        *,
        force_refresh: bool = False,
    ) -> CredentialT: ...


class ValidationGate(Protocol):
    """Port for persistent NEEDS_VALIDATION state."""

    async def inspect(self, account_id: str) -> ValidationStatus: ...

    async def mark_needs_validation(self, account_id: str) -> None: ...

    async def clear_after_refresh(self, account_id: str) -> bool: ...
