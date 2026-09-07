"""Explicit confirmation contract for potentially consequential commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .identifiers import CorrelationId
from .time import ensure_utc, utc_now


@dataclass(frozen=True, slots=True)
class ConfirmationChallenge:
    challenge_id: str
    action: str
    message: str
    correlation_id: CorrelationId
    expires_at: datetime
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        challenge_id = self.challenge_id.strip()
        action = self.action.strip()
        message = self.message.strip()
        if not challenge_id or not action or not message:
            raise ValueError("confirmation challenge fields must not be blank")
        object.__setattr__(self, "challenge_id", challenge_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "expires_at", ensure_utc(self.expires_at))
        object.__setattr__(self, "details", dict(self.details))

    @classmethod
    def create(
        cls,
        *,
        action: str,
        message: str,
        correlation_id: CorrelationId,
        ttl: timedelta = timedelta(minutes=5),
        details: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ConfirmationChallenge:
        if ttl <= timedelta(0):
            raise ValueError("confirmation ttl must be positive")
        base = ensure_utc(now) if now is not None else utc_now()
        return cls(
            challenge_id=uuid4().hex,
            action=action,
            message=message,
            correlation_id=correlation_id,
            expires_at=base + ttl,
            details=dict(details or {}),
        )

    def is_expired(self, *, now: datetime | None = None) -> bool:
        current = ensure_utc(now) if now is not None else utc_now()
        return current >= self.expires_at
