"""SQLAlchemy implementation of the transactional outbox repository."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
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
            .order_by(
                TransactionalOutbox.attempt_count.asc(),
                TransactionalOutbox.occurred_at.asc(),
                TransactionalOutbox.id.asc(),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [self._to_record(row) for row in rows]

    async def claim_pending(
        self,
        *,
        owner: str,
        lease_duration: timedelta,
        limit: int = 100,
        now: datetime | None = None,
    ) -> Sequence[OutboxRecord]:
        """按 CAS 抢占待发布事件,返回带 lease_token 的记录。

        与 scheduler_work 的 claim_due 同一套路:先扫候选,再逐条条件 UPDATE 抢租约,
        回读校验 token 才算抢到。不依赖 FOR UPDATE SKIP LOCKED,SQLite/MySQL/Postgres 通用。
        """
        if not owner:
            msg = "owner must not be empty"
            raise ValueError(msg)
        if lease_duration <= timedelta(0):
            msg = "lease_duration must be positive"
            raise ValueError(msg)
        if limit <= 0:
            return ()
        current = now or datetime.now(UTC)
        leased_until = current + lease_duration
        claimed: list[OutboxRecord] = []
        candidates = (
            (
                await self._session.execute(
                    select(TransactionalOutbox.id)
                    .where(
                        TransactionalOutbox.published_at.is_(None),
                        or_(
                            TransactionalOutbox.lease_token.is_(None),
                            TransactionalOutbox.leased_until <= current,
                        ),
                    )
                    .order_by(
                        TransactionalOutbox.attempt_count.asc(),
                        TransactionalOutbox.occurred_at.asc(),
                        TransactionalOutbox.id.asc(),
                    )
                    .limit(limit * 4)
                )
            )
            .scalars()
            .all()
        )
        for row_id in candidates:
            if len(claimed) >= limit:
                break
            token = uuid4().hex
            result = await self._session.execute(
                update(TransactionalOutbox)
                .where(
                    TransactionalOutbox.id == row_id,
                    TransactionalOutbox.published_at.is_(None),
                    or_(
                        TransactionalOutbox.lease_token.is_(None),
                        TransactionalOutbox.leased_until <= current,
                    ),
                )
                .values(lease_token=token, lease_owner=owner, leased_until=leased_until)
            )
            if not result.rowcount:
                continue
            await self._session.flush()
            row = await self._session.get(TransactionalOutbox, row_id)
            if row is not None and row.lease_token == token:
                claimed.append(replace(self._to_record(row), lease_token=token))
        return tuple(claimed)

    async def release_claim(self, event_id: str, *, lease_token: str) -> bool:
        """放弃租约(例如记录已被别的 worker 处理),让事件立即可被再次抢占。"""
        result = await self._session.execute(
            update(TransactionalOutbox)
            .where(
                TransactionalOutbox.event_id == event_id,
                TransactionalOutbox.published_at.is_(None),
                TransactionalOutbox.lease_token == lease_token,
            )
            .values(lease_token=None, lease_owner=None, leased_until=None)
        )
        return bool(result.rowcount)

    async def mark_published(
        self, event_id: str, *, published_at: datetime, lease_token: str | None = None
    ) -> bool:
        """标记已发布;带 lease_token 时必须同时持有该租约(CAS)。"""
        conditions = [
            TransactionalOutbox.event_id == event_id,
            TransactionalOutbox.published_at.is_(None),
        ]
        if lease_token is not None:
            conditions.append(TransactionalOutbox.lease_token == lease_token)
        result = await self._session.execute(
            update(TransactionalOutbox)
            .where(*conditions)
            .values(
                published_at=published_at,
                last_error=None,
                lease_token=None,
                lease_owner=None,
                leased_until=None,
            )
        )
        return bool(result.rowcount)

    async def record_failure(
        self, event_id: str, *, error: str, lease_token: str | None = None
    ) -> bool:
        """记录失败并释放租约,让下一次抢占可以重试。"""
        conditions = [
            TransactionalOutbox.event_id == event_id,
            TransactionalOutbox.published_at.is_(None),
        ]
        if lease_token is not None:
            conditions.append(TransactionalOutbox.lease_token == lease_token)
        result = await self._session.execute(
            update(TransactionalOutbox)
            .where(*conditions)
            .values(
                attempt_count=TransactionalOutbox.attempt_count + 1,
                last_error=error,
                lease_token=None,
                lease_owner=None,
                leased_until=None,
            )
        )
        return bool(result.rowcount)

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
            lease_token=row.lease_token,
        )
