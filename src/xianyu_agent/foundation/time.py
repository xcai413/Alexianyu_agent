"""UTC-only persistence time contract."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return an aware UTC timestamp suitable for persistence."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC; reject ambiguous naive values."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)
