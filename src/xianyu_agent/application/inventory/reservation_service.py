"""预留的应用层编排(Issue #5 Phase 6, M15/M16)。

每个方法自己是一个事务:reserve 要么整批成功,要么一行都不留——并发冲突时抛
ConcurrentReservationError,由关闭 session 隐式回滚,调用方看到的是明确的失败而不是
半份库存。
"""

from __future__ import annotations

from datetime import datetime

from xianyu_agent.db import get_async_session
from xianyu_agent.domain.inventory import Reservation
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyInventoryRepository


class InventoryReservationService:
    """Reserve, release, consume and resolve inventory units."""

    async def reserve(
        self, pool_id: int, job_ref: str, quantity: int, *, now: datetime | None = None
    ) -> Reservation:
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            reservation = await repo.reserve(pool_id, job_ref, quantity, now=now)
            await session.commit()
        return reservation

    async def release(self, reservation_id: int, *, now: datetime | None = None) -> int:
        """明确未发送时退回库存。"""
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            released = await repo.release(reservation_id, now=now)
            await session.commit()
        return released

    async def consume(self, reservation_id: int, *, now: datetime | None = None) -> int:
        """确认发送成功后核销库存。"""
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            consumed = await repo.consume(reservation_id, now=now)
            await session.commit()
        return consumed

    async def mark_uncertain(self, reservation_id: int) -> int:
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            frozen = await repo.mark_uncertain(reservation_id)
            await session.commit()
        return frozen

    async def resolve_uncertain(
        self, reservation_id: int, *, delivered: bool, now: datetime | None = None
    ) -> int:
        """人工或对账给出的结论:delivered 表示外部确实已经收到。"""
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            resolved = await repo.resolve_uncertain(reservation_id, delivered=delivered, now=now)
            await session.commit()
        return resolved

    async def get(self, reservation_id: int) -> Reservation:
        async with get_async_session() as session:
            reservation = await SqlAlchemyInventoryRepository(session).get_reservation(
                reservation_id
            )
        if reservation is None:
            msg = f"预留 #{reservation_id} 不存在"
            raise ValueError(msg)
        return reservation

    async def contents(self, reservation_id: int) -> tuple[str, ...]:
        """已预留单元的真实卡密正文,只允许发货执行器使用。

        返回值里是明文卡密:调用方不得写日志、写审计正文或放进异常信息;审计只记单元 id 与
        掩码预览(见 Issue #5 §27)。
        """
        async with get_async_session() as session:
            units = await SqlAlchemyInventoryRepository(session).units_of_reservation(
                reservation_id
            )
        return tuple(unit.content for unit in units)
