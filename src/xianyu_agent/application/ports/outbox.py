"""Transactional outbox contracts consumed by application/UoW code."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    """Persistence-neutral event envelope inserted with a business transaction."""

    event_id: str
    event_type: str
    version: int
    correlation_id: str
    payload: dict[str, object]
    occurred_at: datetime
    account_id: str | None = None
    aggregate_type: str | None = None
    aggregate_id: str | None = None
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """Durable projection used by a future outbox dispatcher."""

    id: int
    event_id: str
    event_type: str
    version: int
    account_id: str | None
    aggregate_type: str | None
    aggregate_id: str | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, object]
    occurred_at: datetime
    published_at: datetime | None
    attempt_count: int
    last_error: str | None


class OutboxRepository(Protocol):
    """Transactional persistence contract; methods never commit on their own."""

    async def enqueue(self, event: OutboxEvent) -> OutboxRecord: ...

    async def get_by_event_id(self, event_id: str) -> OutboxRecord | None: ...

    async def list_pending(self, *, limit: int = 100) -> Sequence[OutboxRecord]: ...

    async def mark_published(self, event_id: str, *, published_at: datetime) -> bool: ...

    async def record_failure(self, event_id: str, *, error: str) -> bool: ...
