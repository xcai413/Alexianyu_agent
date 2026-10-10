"""SQLAlchemy 库存仓储:库存池、可消费单元与原子预留(Issue #5 Phase 6)。

事务由调用方拥有:这些方法只在当前事务里读写,提交或回滚由应用服务决定,这样才能把
"预留 + 创建发货 job"放进同一个事务。

原子预留用比较并设置实现:先按 id 顺序读候选单元,再逐个执行
UPDATE ... WHERE id=? AND state='available',任何一行影响行数为 0 就抛
ConcurrentReservationError,由调用方整体回滚。三库语义一致,不依赖 SELECT ... FOR UPDATE
(SQLite 不支持),也不依赖数据库的脏读行为。

单元内容属于敏感数据:本模块的日志与异常里只出现单元 id,不出现卡密正文。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.domain.inventory.pool import (
    InventoryPool,
    UnitImportSummary,
    plan_unit_import,
)
from xianyu_agent.domain.inventory.reservation import (
    ConcurrentReservationError,
    InsufficientStock,
    Reservation,
    plan_reservation,
)
from xianyu_agent.domain.inventory.state import (
    CONSUMABLE_POOL_TYPES,
    InventoryUnitState,
    ReservationState,
)
from xianyu_agent.domain.inventory.unit import InventoryUnit
from xianyu_agent.infrastructure.database.models import (
    InventoryPool as InventoryPoolRow,
    InventoryReservation as InventoryReservationRow,
    InventoryUnit as InventoryUnitRow,
)

DEFAULT_LIST_LIMIT = 200


def _to_pool(row: InventoryPoolRow) -> InventoryPool:
    return InventoryPool(
        name=row.name,
        type=row.type,
        description=row.description,
        enabled=row.enabled,
        delay_seconds=row.delay_seconds,
        account_scope=row.account_scope,
        item_scope=row.item_scope,
        sku_scope=row.sku_scope,
        api_config_ref=row.api_config_ref,
        content=row.content,
        image_url=row.image_url,
        local_id=row.id,
    )


def _to_unit(row: InventoryUnitRow) -> InventoryUnit:
    return InventoryUnit(
        pool_id=row.pool_id,
        content=row.content,
        state=row.state,
        reserved_by=row.reserved_by,
        reservation_id=row.reservation_id,
        reserved_at=row.reserved_at,
        consumed_at=row.consumed_at,
        external_ref=row.external_ref,
        metadata=dict(row.unit_metadata or {}),
        local_id=row.id,
    )


def _to_reservation(row: InventoryReservationRow, unit_ids: Sequence[int] = ()) -> Reservation:
    return Reservation(
        pool_id=row.pool_id,
        job_ref=row.job_ref,
        quantity=row.quantity,
        unit_ids=tuple(unit_ids),
        state=row.state,
        local_id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyInventoryRepository:
    """Inventory repository backed by one caller-owned AsyncSession."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- 库存池 ----------------------------------------------------------

    async def create_pool(self, pool: InventoryPool) -> int:
        row = InventoryPoolRow(
            name=pool.name,
            type=pool.type,
            description=pool.description,
            enabled=pool.enabled,
            delay_seconds=pool.delay_seconds,
            account_scope=pool.account_scope,
            item_scope=pool.item_scope,
            sku_scope=pool.sku_scope,
            api_config_ref=pool.api_config_ref,
            content=pool.content,
            image_url=pool.image_url,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def get_pool(self, pool_id: int) -> InventoryPool | None:
        row = await self._session.get(InventoryPoolRow, pool_id)
        return None if row is None else _to_pool(row)

    async def list_pools(
        self,
        *,
        only_enabled: bool = False,
        type_: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[InventoryPool]:
        statement = select(InventoryPoolRow).order_by(InventoryPoolRow.id)
        if only_enabled:
            statement = statement.where(InventoryPoolRow.enabled.is_(True))
        if type_ is not None:
            statement = statement.where(InventoryPoolRow.type == type_)
        rows = (await self._session.scalars(statement.limit(limit))).all()
        return [_to_pool(row) for row in rows]

    async def set_pool_enabled(self, pool_id: int, *, enabled: bool) -> None:
        result = await self._session.execute(
            update(InventoryPoolRow).where(InventoryPoolRow.id == pool_id).values(enabled=enabled)
        )
        if cast(Any, result).rowcount != 1:
            msg = f"库存池 #{pool_id} 不存在"
            raise ValueError(msg)

    async def apply_pool(self, pool: InventoryPool) -> None:
        """Persist the editable fields of an already validated domain pool."""
        if pool.local_id is None:
            msg = "覆盖库存池需要 local_id"
            raise ValueError(msg)
        result = await self._session.execute(
            update(InventoryPoolRow)
            .where(InventoryPoolRow.id == pool.local_id)
            .values(
                name=pool.name,
                description=pool.description,
                enabled=pool.enabled,
                delay_seconds=pool.delay_seconds,
                account_scope=pool.account_scope,
                item_scope=pool.item_scope,
                sku_scope=pool.sku_scope,
                content=pool.content,
                image_url=pool.image_url,
            )
        )
        if cast(Any, result).rowcount != 1:
            msg = f"库存池 #{pool.local_id} 不存在"
            raise ValueError(msg)

    async def delete_pool(self, pool_id: int) -> None:
        result = await self._session.execute(
            delete(InventoryPoolRow).where(InventoryPoolRow.id == pool_id)
        )
        if cast(Any, result).rowcount != 1:
            msg = f"库存池 #{pool_id} 不存在"
            raise ValueError(msg)

    # ---- 库存单元 --------------------------------------------------------

    async def import_units(self, pool_id: int, contents: Sequence[str]) -> UnitImportSummary:
        """追加逐行卡密:空白行与重复行跳过,返回导入统计。"""
        pool = await self._session.get(InventoryPoolRow, pool_id)
        if pool is None:
            msg = f"库存池 #{pool_id} 不存在"
            raise ValueError(msg)
        if pool.type not in CONSUMABLE_POOL_TYPES:
            msg = f"库存池 #{pool_id} 是 {pool.type} 类型,只有 data 池按单元存卡密"
            raise ValueError(msg)
        existing = (
            await self._session.scalars(
                select(InventoryUnitRow.content).where(InventoryUnitRow.pool_id == pool_id)
            )
        ).all()
        accepted, summary = plan_unit_import(contents, existing)
        for content in accepted:
            self._session.add(
                InventoryUnitRow(
                    pool_id=pool_id,
                    content=content,
                    state=InventoryUnitState.AVAILABLE.value,
                )
            )
        if accepted:
            await self._session.flush()
        return summary

    async def pool_counts(self, pool_id: int) -> dict[str, int]:
        rows = (
            await self._session.execute(
                select(InventoryUnitRow.state, func.count())
                .where(InventoryUnitRow.pool_id == pool_id)
                .group_by(InventoryUnitRow.state)
            )
        ).all()
        counts = {state.value: 0 for state in InventoryUnitState}
        for state, count in rows:
            counts[state] = int(count)
        counts["total"] = sum(counts[state.value] for state in InventoryUnitState)
        return counts

    async def list_units(
        self,
        pool_id: int,
        *,
        state: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[InventoryUnit]:
        statement = (
            select(InventoryUnitRow)
            .where(InventoryUnitRow.pool_id == pool_id)
            .order_by(InventoryUnitRow.id)
            .limit(limit)
        )
        if state is not None:
            statement = statement.where(InventoryUnitRow.state == state)
        rows = (await self._session.scalars(statement)).all()
        return [_to_unit(row) for row in rows]

    async def units_of_reservation(self, reservation_id: int) -> list[InventoryUnit]:
        rows = (
            await self._session.scalars(
                select(InventoryUnitRow)
                .where(InventoryUnitRow.reservation_id == reservation_id)
                .order_by(InventoryUnitRow.id)
            )
        ).all()
        return [_to_unit(row) for row in rows]

    # ---- 预留 ------------------------------------------------------------

    async def reserve(
        self,
        pool_id: int,
        job_ref: str,
        quantity: int,
        *,
        now: datetime | None = None,
    ) -> Reservation:
        """按数量整批占用单元,不足或并发冲突就整体失败。"""
        pool = await self._session.get(InventoryPoolRow, pool_id)
        if pool is None:
            msg = f"库存池 #{pool_id} 不存在"
            raise ValueError(msg)
        if not pool.enabled:
            msg = f"库存池 #{pool_id} 已停用,不能预留"
            raise ValueError(msg)
        if pool.type not in CONSUMABLE_POOL_TYPES:
            msg = f"库存池 #{pool_id} 是 {pool.type} 类型,不按单元预留"
            raise ValueError(msg)
        at = now or datetime.now(UTC)
        candidates = (
            await self._session.scalars(
                select(InventoryUnitRow.id)
                .where(
                    InventoryUnitRow.pool_id == pool_id,
                    InventoryUnitRow.state == InventoryUnitState.AVAILABLE.value,
                )
                .order_by(InventoryUnitRow.id)
            )
        ).all()
        try:
            chosen = plan_reservation(candidates, quantity)
        except InsufficientStock as exc:
            raise InsufficientStock(
                requested=exc.requested, available=exc.available, pool_id=pool_id
            ) from exc

        row = InventoryReservationRow(
            pool_id=pool_id,
            job_ref=job_ref,
            quantity=quantity,
            state=ReservationState.RESERVED.value,
        )
        self._session.add(row)
        await self._session.flush()
        # MySQL 的 INSERT 不会在这里回传非主键的服务端默认值。若 _to_reservation
        # 同步读取 created_at/updated_at,ORM 会隐式发起异步 SELECT 并触发
        # MissingGreenlet;因此在仓储的 await 上下文中显式加载。
        await self._session.refresh(row, attribute_names=("created_at", "updated_at"))

        for unit_id in chosen:
            result = await self._session.execute(
                update(InventoryUnitRow)
                .where(
                    InventoryUnitRow.id == unit_id,
                    InventoryUnitRow.state == InventoryUnitState.AVAILABLE.value,
                )
                .values(
                    state=InventoryUnitState.RESERVED.value,
                    reservation_id=row.id,
                    reserved_by=job_ref,
                    reserved_at=at,
                )
            )
            if cast(Any, result).rowcount != 1:
                msg = (
                    f"库存单元 #{unit_id} 在预留过程中被其他事务占用,"
                    f"本次预留(池 #{pool_id} job={job_ref})需要整体回滚"
                )
                raise ConcurrentReservationError(msg)
        return _to_reservation(row, chosen)

    async def get_reservation(self, reservation_id: int) -> Reservation | None:
        row = await self._session.get(InventoryReservationRow, reservation_id)
        if row is None:
            return None
        unit_ids = (
            await self._session.scalars(
                select(InventoryUnitRow.id)
                .where(InventoryUnitRow.reservation_id == reservation_id)
                .order_by(InventoryUnitRow.id)
            )
        ).all()
        return _to_reservation(row, unit_ids)

    async def list_reservations(
        self,
        *,
        job_ref: str | None = None,
        state: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[Reservation]:
        statement = (
            select(InventoryReservationRow).order_by(InventoryReservationRow.id).limit(limit)
        )
        if job_ref is not None:
            statement = statement.where(InventoryReservationRow.job_ref == job_ref)
        if state is not None:
            statement = statement.where(InventoryReservationRow.state == state)
        rows = (await self._session.scalars(statement)).all()
        return [_to_reservation(row) for row in rows]

    async def _load_for_transition(
        self, reservation_id: int
    ) -> tuple[Reservation, InventoryReservationRow]:
        row = await self._session.get(InventoryReservationRow, reservation_id)
        if row is None:
            msg = f"预留 #{reservation_id} 不存在"
            raise ValueError(msg)
        return _to_reservation(row), row

    async def _set_units_state(
        self,
        reservation_id: int,
        *,
        from_states: Sequence[str],
        target: str,
        at: datetime | None = None,
    ) -> int:
        values: dict[str, object] = {"state": target}
        if target == InventoryUnitState.AVAILABLE.value:
            values |= {"reservation_id": None, "reserved_by": None, "reserved_at": None}
        if target == InventoryUnitState.CONSUMED.value:
            values["consumed_at"] = at or datetime.now(UTC)
        result = await self._session.execute(
            update(InventoryUnitRow)
            .where(
                InventoryUnitRow.reservation_id == reservation_id,
                InventoryUnitRow.state.in_(tuple(from_states)),
            )
            .values(**values)
        )
        return int(cast(Any, result).rowcount or 0)

    async def release(self, reservation_id: int, *, now: datetime | None = None) -> int:
        """明确未发送时把单元还给池;只能从 reserved 或 uncertain 走这一步。"""
        reservation, row = await self._load_for_transition(reservation_id)
        reservation.transition_to(ReservationState.RELEASED.value)
        released = await self._set_units_state(
            reservation_id,
            from_states=(
                InventoryUnitState.RESERVED.value,
                InventoryUnitState.UNCERTAIN.value,
            ),
            target=InventoryUnitState.AVAILABLE.value,
        )
        row.state = ReservationState.RELEASED.value
        row.updated_at = now or datetime.now(UTC)
        await self._session.flush()
        return released

    async def consume(self, reservation_id: int, *, now: datetime | None = None) -> int:
        """确认发送成功后把单元标记为已消费。"""
        reservation, row = await self._load_for_transition(reservation_id)
        reservation.transition_to(ReservationState.CONSUMED.value)
        consumed = await self._set_units_state(
            reservation_id,
            from_states=(
                InventoryUnitState.RESERVED.value,
                InventoryUnitState.UNCERTAIN.value,
            ),
            target=InventoryUnitState.CONSUMED.value,
            at=now,
        )
        row.state = ReservationState.CONSUMED.value
        row.updated_at = now or datetime.now(UTC)
        await self._session.flush()
        return consumed

    async def mark_uncertain(self, reservation_id: int) -> int:
        """冻结预留:外部可能已经产生副作用,既不能当成功也不能当没发生。"""
        reservation, row = await self._load_for_transition(reservation_id)
        reservation.transition_to(ReservationState.UNCERTAIN.value)
        units = await self._set_units_state(
            reservation_id,
            from_states=(InventoryUnitState.RESERVED.value,),
            target=InventoryUnitState.UNCERTAIN.value,
        )
        row.state = ReservationState.UNCERTAIN.value
        await self._session.flush()
        return units

    async def resolve_uncertain(
        self, reservation_id: int, *, delivered: bool, now: datetime | None = None
    ) -> int:
        """人工或对账给出的结论:确实发出去了就消费,确实没发就退回。"""
        if delivered:
            return await self.consume(reservation_id, now=now)
        return await self.release(reservation_id, now=now)
