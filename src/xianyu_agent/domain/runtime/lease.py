"""数据库支持的集群级单例任务租约。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError

from xianyu_agent.db import RuntimeLease, get_async_session


async def claim(
    name: str,
    *,
    owner_id: str,
    ttl: timedelta,
    allow_owner_renewal: bool = True,
    now: datetime | None = None,
) -> bool:
    """以 CAS 取得或续租命名任务;不同数据库使用同一语义。"""
    if not name or not owner_id:
        msg = "lease name and owner_id must not be empty"
        raise ValueError(msg)
    if ttl.total_seconds() <= 0:
        msg = "lease ttl must be positive"
        raise ValueError(msg)
    current = _as_utc(now or datetime.now(UTC))
    async with get_async_session() as session:
        availability = RuntimeLease.lease_until <= current
        if allow_owner_renewal:
            availability = or_(RuntimeLease.owner_id == owner_id, availability)
        result = await session.execute(
            update(RuntimeLease)
            .where(RuntimeLease.name == name)
            .where(availability)
            .values(
                owner_id=owner_id,
                lease_until=current + ttl,
                heartbeat_at=current,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            await session.commit()
            return True
        session.add(
            RuntimeLease(
                name=name,
                owner_id=owner_id,
                lease_until=current + ttl,
                heartbeat_at=current,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
        return True


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
