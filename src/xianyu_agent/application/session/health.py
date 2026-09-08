"""Credential lifecycle health and result contracts.

These types are intentionally transport-neutral. Secret-bearing material is wrapped in
``CredentialHandle`` so routine repr/logging of application results cannot expose it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

CredentialT = TypeVar("CredentialT")


class CredentialHealthState(StrEnum):
    """Normalized health of one account's credential chain."""

    MISSING = "missing"
    UNUSABLE = "unusable"
    REFRESH_REQUIRED = "refresh_required"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    HEALTHY = "healthy"
    NEEDS_VALIDATION = "needs_validation"
    ERROR = "error"


class CredentialResultState(StrEnum):
    """Stable orchestration result consumed by future runtime/recovery layers."""

    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    NEEDS_VALIDATION = "needs_validation"
    TERMINAL_FAILURE = "terminal_failure"


@dataclass(frozen=True)
class CredentialHealth:
    """Safe-to-log credential health summary."""

    account_id: str
    state: CredentialHealthState
    expires_at: datetime | None = None
    code: str | None = None
    refresh_recommended: bool = False
    validation_cooling: bool = False


@dataclass(frozen=True)
class CredentialHandle(Generic[CredentialT]):
    """Explicit wrapper around secret-bearing credential material."""

    _value: CredentialT = field(repr=False)

    def unwrap(self) -> CredentialT:
        """Return secret-bearing material to an authorized integration boundary."""
        return self._value


@dataclass(frozen=True)
class CredentialResult(Generic[CredentialT]):
    """One CredentialSupervisor operation result.

    The credential handle is excluded from repr/compare so token-like material does not
    accidentally enter diagnostics, snapshots, or assertion failure output.
    """

    state: CredentialResultState
    health: CredentialHealth
    credential: CredentialHandle[CredentialT] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    refreshed: bool = False
    code: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is CredentialResultState.SUCCESS
