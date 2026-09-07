"""SQLAlchemy implementation of the transactional outbox repository."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.application.ports.outbox import OutboxEvent, OutboxRecord
from xianyu_agent.infrastructure.database.models import TransactionalOutbox


class SqlAlchemyOutboxRepository:
    """Outbox repository backed by one caller-owned ``AsyncSession``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, event: OutboxEvent) -> OutboxRecord:
        row = TransactionalOutbox(
            event_id=event.event_id,
            event_type=event.event_type,
            version=event.version,
            account_id=event.account_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return self._to_record(row)

    async def get_by_event_id(self, event_id: str) -> OutboxRecord | None:
        row = await self._get_model(event_id)
        return None if row is None else self._to_record(row)

    async def list_pending(self, *, limit: int = 100) -> Sequence[OutboxRecord]:
        if limit <= 0:
            return []
        stmt = (
            select(TransactionalOutbox)
            .where(TransactionalOutbox.published_at.is_(None))
            .order_by(TransactionalOutbox.occurred_at.asc(), TransactionalOutbox.id.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_record(row) for row in rows]

    async def mark_published(self, event_id: str, *, published_at: datetime) -> bool:
        row = await self._get_model(event_id)
        if row is None:
            return False
        row.published_at = published_at
        row.last_error = None
        await self._session.flush()
        return True

    async def record_failure(self, event_id: str, *, error: str) -> bool:
        row = await self._get_model(event_id)
        if row is None:
            return False
        row.attempt_count += 1
        row.last_error = error
        await self._session.flush()
        return True

    async def _get_model(self, event_id: str) -> TransactionalOutbox | None:
        stmt = select(TransactionalOutbox).where(TransactionalOutbox.event_id == event_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _to_record(row: TransactionalOutbox) -> OutboxRecord:
        return OutboxRecord(
            id=row.id,
            event_id=row.event_id,
            event_type=row.event_type,
            version=row.version,
            account_id=row.account_id,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            correlation_id=row.correlation_id,
            causation_id=row.causation_id,
            payload=dict(row.payload),
            occurred_at=row.occurred_at,
            published_at=row.published_at,
            attempt_count=row.attempt_count,
            last_error=row.last_error,
        )
