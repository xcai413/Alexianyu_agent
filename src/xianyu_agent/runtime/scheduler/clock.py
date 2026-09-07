"""Clock primitives for scheduler code."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Return an aware UTC timestamp."""

    def now(self) -> datetime: ...


class SystemClock:
    """Production wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def require_utc(value: datetime) -> datetime:
    """Reject naive or non-UTC scheduler timestamps."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler timestamps must be timezone-aware UTC values")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("scheduler timestamps must use UTC")
    return value
