"""库存原子预留的三库一致性契约(Issue #5 §21)。

并发预留不允许双花同一条单元:两个事务同时看到"还有 3 条空闲"时只能有一个成功,另一个必须
整体失败,而不是发出半份库存。这条契约在 SQLite / MySQL / PostgreSQL 上必须一致,因此由
CI 的数据库矩阵执行(本地无后端时跳过)。
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from xianyu_agent.config import get_settings
from xianyu_agent.db import database as legacy_database
from xianyu_agent.domain.inventory import InventoryPool
from xianyu_agent.domain.inventory.reservation import (
    ConcurrentReservationError,
    InsufficientStock,
)
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyInventoryRepository


async def _create_pool(units: list[str]) -> int:
    async with legacy_database.async_session_factory() as session, session.begin():
        repo = SqlAlchemyInventoryRepository(session)
        pool_id = await repo.create_pool(
            InventoryPool(name=f"并发池-{uuid4().hex[:8]}", type="data")
        )
        await repo.import_units(pool_id, units)
        return pool_id


async def _counts(pool_id: int) -> dict[str, int]:
    async with legacy_database.async_session_factory() as session:
        return await SqlAlchemyInventoryRepository(session).pool_counts(pool_id)


async def _assert_concurrent_reserve_never_double_spends() -> None:
    pool_id = await _create_pool(["C1", "C2", "C3", "C4"])

    async def attempt(job_ref: str) -> object:
        async with legacy_database.async_session_factory() as session:
            repo = SqlAlchemyInventoryRepository(session)
            try:
                async with session.begin():
                    return await repo.reserve(pool_id, job_ref, 3)
            except (InsufficientStock, ConcurrentReservationError) as exc:
                return exc

    results = await asyncio.gather(*(attempt(f"job-{index}") for index in range(3)))

    winners = [result for result in results if not isinstance(result, BaseException)]
    losers = [result for result in results if isinstance(result, BaseException)]

    assert len(winners) == 1
    assert len(losers) == 2
    counts = await _counts(pool_id)
    assert counts["reserved"] == 3
    assert counts["available"] == 1
    assert counts["consumed"] == 0


async def _assert_multi_quantity_reservation_is_all_or_nothing() -> None:
    pool_id = await _create_pool(["D1", "D2"])

    async with legacy_database.async_session_factory() as session, session.begin():
        repo = SqlAlchemyInventoryRepository(session)
        with pytest.raises(InsufficientStock) as excinfo:
            await repo.reserve(pool_id, "job-short", 3)
        assert excinfo.value.available == 2

    counts = await _counts(pool_id)
    assert counts == {"available": 2, "reserved": 0, "consumed": 0, "uncertain": 0, "total": 2}

    async with legacy_database.async_session_factory() as session:
        assert (
            await SqlAlchemyInventoryRepository(session).list_reservations(job_ref="job-short")
            == []
        )


async def _assert_reserved_units_are_not_reused_sequentially() -> None:
    pool_id = await _create_pool(["E1", "E2"])

    async with legacy_database.async_session_factory() as session, session.begin():
        repo = SqlAlchemyInventoryRepository(session)
        first = await repo.reserve(pool_id, "job-1", 2)

    async with legacy_database.async_session_factory() as session, session.begin():
        repo = SqlAlchemyInventoryRepository(session)
        with pytest.raises(InsufficientStock):
            await repo.reserve(pool_id, "job-2", 1)

    async with legacy_database.async_session_factory() as session, session.begin():
        repo = SqlAlchemyInventoryRepository(session)
        await repo.release(first.local_id)

    async with legacy_database.async_session_factory() as session, session.begin():
        repo = SqlAlchemyInventoryRepository(session)
        second = await repo.reserve(pool_id, "job-2", 1)
        assert second.unit_ids == first.unit_ids[:1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inventory_reservation_contract_across_backend() -> None:
    """Exercise every backend assertion on one asyncio event loop."""
    backend = os.environ.get("TEST_DATABASE_BACKEND")
    if not backend:
        pytest.skip("TEST_DATABASE_BACKEND 仅由数据库兼容性 CI job 提供")
    assert get_settings().database_backend == backend

    await _assert_concurrent_reserve_never_double_spends()
    await _assert_multi_quantity_reservation_is_all_or_nothing()
    await _assert_reserved_units_are_not_reused_sequentially()
