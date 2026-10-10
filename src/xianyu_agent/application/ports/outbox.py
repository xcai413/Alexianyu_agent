"""Transactional outbox contracts consumed by application/UoW code."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from xianyu_agent.foundation import CausationId, CorrelationId


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

    def __post_init__(self) -> None:
        correlation_id = CorrelationId(self.correlation_id)
        causation_id = None if self.causation_id is None else CausationId(self.causation_id)
        object.__setattr__(self, "correlation_id", correlation_id.value)
        object.__setattr__(
            self,
            "causation_id",
            None if causation_id is None else causation_id.value,
        )


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
    # 抢占租约:只由 claim_pending 填充,用于发布/失败时的 CAS 校验。
    lease_token: str | None = None


class OutboxRepository(Protocol):
    """Transactional persistence contract; methods never commit on their own."""

    async def enqueue(self, event: OutboxEvent) -> OutboxRecord: ...

    async def get_by_event_id(self, event_id: str) -> OutboxRecord | None: ...

    async def list_pending(self, *, limit: int = 100) -> Sequence[OutboxRecord]: ...

    async def claim_pending(
        self,
        *,
        owner: str,
        lease_duration: timedelta,
        limit: int = 100,
        now: datetime,
    ) -> Sequence[OutboxRecord]: ...

    async def release_claim(self, event_id: str, *, lease_token: str) -> bool: ...

    async def mark_published(
        self, event_id: str, *, published_at: datetime, lease_token: str | None = None
    ) -> bool: ...

    async def record_failure(
        self, event_id: str, *, error: str, lease_token: str | None = None
    ) -> bool: ...
