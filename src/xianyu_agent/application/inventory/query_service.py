"""库存查询视图(Issue #5 Phase 6, 6B)。

查询层只读,并且默认只给出卡密的掩码预览:完整卡密正文属于敏感数据,只有发货执行器需要,
那条路径由 InventoryReservationService.contents 单独提供。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from xianyu_agent.db import get_async_session
from xianyu_agent.domain.inventory import InventoryPool, InventoryUnit, Reservation
from xianyu_agent.domain.inventory.unit import masked_preview
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyInventoryRepository

DEFAULT_LIMIT = 200


@dataclass(frozen=True)
class PoolView:
    """A pool plus its current unit counts."""

    pool: InventoryPool
    counts: Mapping[str, int]

    @property
    def available(self) -> int:
        return int(self.counts.get("available", 0))

    @property
    def reserved(self) -> int:
        return int(self.counts.get("reserved", 0))

    @property
    def uncertain(self) -> int:
        return int(self.counts.get("uncertain", 0))

    @property
    def total(self) -> int:
        return int(self.counts.get("total", 0))


@dataclass(frozen=True)
class UnitView:
    """A unit as operators see it: masked content, no raw code."""

    unit_id: int
    pool_id: int
    state: str
    preview: str
    reserved_by: str | None = None
    reservation_id: int | None = None
    reserved_at: datetime | None = None
    consumed_at: datetime | None = None

    @classmethod
    def from_unit(cls, unit: InventoryUnit) -> UnitView:
        return cls(
            unit_id=unit.local_id or 0,
            pool_id=unit.pool_id,
            state=unit.state,
            preview=masked_preview(unit.content),
            reserved_by=unit.reserved_by,
            reservation_id=unit.reservation_id,
            reserved_at=unit.reserved_at,
            consumed_at=unit.consumed_at,
        )


class InventoryQueryService:
    """Read-only views over pools, units and reservations."""

    async def list_pools(
        self,
        *,
        only_enabled: bool = False,
        type_: str | None = None,
        with_counts: bool = True,
        limit: int = DEFAULT_LIMIT,
    ) -> list[PoolView]:
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            pools = await repo.list_pools(only_enabled=only_enabled, type_=type_, limit=limit)
            if not with_counts:
                return [PoolView(pool=pool, counts={}) for pool in pools]
            return [
                PoolView(pool=pool, counts=await repo.pool_counts(pool.local_id))
                for pool in pools
                if pool.local_id
            ]

    async def pool_detail(self, pool_id: int) -> PoolView:
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            pool = await repo.get_pool(pool_id)
            if pool is None:
                msg = f"库存池 #{pool_id} 不存在"
                raise ValueError(msg)
            return PoolView(pool=pool, counts=await repo.pool_counts(pool_id))

    async def list_units(
        self,
        pool_id: int,
        *,
        state: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[UnitView]:
        async with get_async_session() as session:
            units = await SqlAlchemyInventoryRepository(session).list_units(
                pool_id, state=state, limit=limit
            )
        return [UnitView.from_unit(unit) for unit in units]

    async def plain_contents(
        self, pool_id: int, *, state: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> tuple[str, ...]:
        """明文卡密,只给人工核对用;调用方不得写日志、审计或异常正文。"""
        async with get_async_session() as session:
            units = await SqlAlchemyInventoryRepository(session).list_units(
                pool_id, state=state, limit=limit
            )
        return tuple(unit.content for unit in units)

    async def list_reservations(
        self,
        *,
        job_ref: str | None = None,
        state: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[Reservation]:
        async with get_async_session() as session:
            return await SqlAlchemyInventoryRepository(session).list_reservations(
                job_ref=job_ref, state=state, limit=limit
            )

    async def reservation_detail(self, reservation_id: int) -> Reservation:
        async with get_async_session() as session:
            reservation = await SqlAlchemyInventoryRepository(session).get_reservation(
                reservation_id
            )
        if reservation is None:
            msg = f"预留 #{reservation_id} 不存在"
            raise ValueError(msg)
        return reservation
