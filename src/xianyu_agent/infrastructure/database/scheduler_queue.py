"""SQLAlchemy implementation of the persistence-neutral Scheduler DueQueue."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from xianyu_agent.db import database as legacy_database
from xianyu_agent.infrastructure.database.models.scheduler import SchedulerWork
from xianyu_agent.runtime.scheduler.clock import require_utc
from xianyu_agent.runtime.scheduler.lease import LeasedWork


class SqlAlchemyDueQueue:
    """Durable scheduler queue using compare-and-set lease ownership.

    Each operation owns its transaction. Claiming deliberately avoids relying on
    backend-specific SKIP LOCKED syntax: candidate rows are selected first and a
    conditional UPDATE acquires each lease. Competing workers may inspect the
    same candidate, but only one can install its unique lease token.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or legacy_database.async_session_factory

    async def enqueue(
        self,
        *,
        work_id: str,
        payload: dict[str, object],
        available_at: datetime,
    ) -> None:
        """Persist opaque work for producers that own scheduling semantics."""
        if not work_id:
            raise ValueError("work_id must not be empty")
        available_at = require_utc(available_at)
        async with self._sessions()() as session, session.begin():
            session.add(
                SchedulerWork(
                    work_id=work_id,
                    payload=dict(payload),
                    available_at=available_at,
                )
            )

    async def claim_due(
        self,
        *,
        now: datetime,
        lease_owner: str,
        lease_duration: timedelta,
        limit: int,
    ) -> tuple[LeasedWork, ...]:
        now = require_utc(now)
        if not lease_owner:
            raise ValueError("lease_owner must not be empty")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if limit <= 0:
            return ()

        leased_until = now + lease_duration
        claimed: list[LeasedWork] = []
        async with self._sessions()() as session, session.begin():
            candidates = (
                await session.execute(
                    select(SchedulerWork.work_id)
                    .where(
                        SchedulerWork.available_at <= now,
                        or_(
                            SchedulerWork.lease_token.is_(None),
                            SchedulerWork.leased_until <= now,
                        ),
                    )
                    .order_by(SchedulerWork.available_at.asc(), SchedulerWork.work_id.asc())
                    .limit(limit * 4)
                )
            ).scalars().all()

            for work_id in candidates:
                if len(claimed) >= limit:
                    break
                token = uuid4().hex
                await session.execute(
                    update(SchedulerWork)
                    .where(
                        SchedulerWork.work_id == work_id,
                        SchedulerWork.available_at <= now,
                        or_(
                            SchedulerWork.lease_token.is_(None),
                            SchedulerWork.leased_until <= now,
                        ),
                    )
                    .values(
                        lease_token=token,
                        lease_owner=lease_owner,
                        leased_until=leased_until,
                    )
                )
                row = (
                    await session.execute(
                        select(SchedulerWork).where(
                            SchedulerWork.work_id == work_id,
                            SchedulerWork.lease_token == token,
                            SchedulerWork.lease_owner == lease_owner,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    claimed.append(self._to_lease(row))

        return tuple(claimed)

    async def complete(self, work: LeasedWork, *, completed_at: datetime) -> bool:
        require_utc(completed_at)
        async with self._sessions()() as session, session.begin():
            await session.execute(
                delete(SchedulerWork).where(
                    SchedulerWork.work_id == work.work_id,
                    SchedulerWork.lease_token == work.lease_token,
                    SchedulerWork.lease_owner == work.lease_owner,
                )
            )
            row = (
                await session.execute(
                    select(SchedulerWork.work_id).where(
                        SchedulerWork.work_id == work.work_id,
                        SchedulerWork.lease_token == work.lease_token,
                        SchedulerWork.lease_owner == work.lease_owner,
                    )
                )
            ).scalar_one_or_none()
            return row is None

    async def release(self, work: LeasedWork, *, available_at: datetime) -> bool:
        available_at = require_utc(available_at)
        async with self._sessions()() as session, session.begin():
            token = uuid4().hex
            await session.execute(
                update(SchedulerWork)
                .where(
                    SchedulerWork.work_id == work.work_id,
                    SchedulerWork.lease_token == work.lease_token,
                    SchedulerWork.lease_owner == work.lease_owner,
                )
                .values(
                    available_at=available_at,
                    lease_token=None,
                    lease_owner=None,
                    leased_until=None,
                )
            )
            # A matching old lease must no longer exist; distinguish stale rows
            # by probing the work id and treating deletion as stale here.
            row = (
                await session.execute(
                    select(SchedulerWork).where(SchedulerWork.work_id == work.work_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            if row.lease_token is None and row.lease_owner is None:
                return True
            return row.lease_token == token

    async def recover_expired(self, *, now: datetime) -> int:
        now = require_utc(now)
        recovered = 0
        async with self._sessions()() as session, session.begin():
            expired_ids = (
                await session.execute(
                    select(SchedulerWork.work_id).where(
                        SchedulerWork.lease_token.is_not(None),
                        SchedulerWork.leased_until <= now,
                    )
                )
            ).scalars().all()
            for work_id in expired_ids:
                marker = uuid4().hex
                await session.execute(
                    update(SchedulerWork)
                    .where(
                        SchedulerWork.work_id == work_id,
                        SchedulerWork.lease_token.is_not(None),
                        SchedulerWork.leased_until <= now,
                    )
                    .values(
                        lease_token=None,
                        lease_owner=None,
                        leased_until=None,
                    )
                )
                row = (
                    await session.execute(
                        select(SchedulerWork).where(SchedulerWork.work_id == work_id)
                    )
                ).scalar_one_or_none()
                if row is not None and row.lease_token is None:
                    recovered += 1
                del marker
        return recovered

    @staticmethod
    def _to_lease(row: SchedulerWork) -> LeasedWork:
        leased_until = row.leased_until
        if leased_until is None or row.lease_token is None or row.lease_owner is None:
            raise RuntimeError("claimed scheduler row is missing lease state")
        if leased_until.tzinfo is None:
            leased_until = leased_until.replace(tzinfo=UTC)
        else:
            leased_until = leased_until.astimezone(UTC)
        return LeasedWork(
            work_id=row.work_id,
            lease_token=row.lease_token,
            lease_owner=row.lease_owner,
            leased_until=leased_until,
            payload=dict(row.payload),
        )
