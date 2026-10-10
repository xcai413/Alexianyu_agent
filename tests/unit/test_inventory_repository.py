"""库存仓储的原子预留行为(SQLite 本地;三库一致性由 integration 用例在 CI 覆盖)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain.inventory import (
    InvalidStateTransition,
    InventoryPool,
    InventoryUnitState,
    ReservationState,
    UnitImportSummary,
)
from xianyu_agent.domain.inventory.reservation import (
    ConcurrentReservationError,
    InsufficientStock,
)
from xianyu_agent.infrastructure.database.repositories import (
    SqlAlchemyInventoryRepository,
    inventory as inventory_repo,
)


@pytest.fixture
async def inventory_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "inventory.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _repo(session) -> SqlAlchemyInventoryRepository:
    return SqlAlchemyInventoryRepository(session)


async def _seed_pool(
    repo: SqlAlchemyInventoryRepository,
    *,
    units: tuple[str, ...] = ("CARD-1", "CARD-2", "CARD-3"),
    type_: str = "data",
    enabled: bool = True,
) -> int:
    pool_id = await repo.create_pool(
        InventoryPool(
            name="测试池",
            type=type_,
            enabled=enabled,
            # 启用的 text 池必须有文案,否则领域校验会拒绝。
            content="结算后发我地址" if type_ == "text" else None,
        )
    )
    if units and type_ == "data":
        await repo.import_units(pool_id, list(units))
    return pool_id


async def test_pool_lifecycle_and_unit_counts(inventory_db) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        pool_id = await _seed_pool(repo, units=("CARD-1", "CARD-2", "", "CARD-1"))

        assert await repo.pool_counts(pool_id) == {
            "available": 2,
            "reserved": 0,
            "consumed": 0,
            "uncertain": 0,
            "total": 2,
        }
        pool = await repo.get_pool(pool_id)
        assert pool is not None
        assert pool.local_id == pool_id
        assert pool.type == "data"
        assert [row.local_id for row in await repo.list_pools()] == [pool_id]

        await repo.set_pool_enabled(pool_id, enabled=False)
        assert await repo.list_pools(only_enabled=True) == []

        await repo.delete_pool(pool_id)
        assert await repo.get_pool(pool_id) is None
        with pytest.raises(ValueError, match="不存在"):
            await repo.set_pool_enabled(pool_id, enabled=True)


async def test_import_units_reports_skips(inventory_db) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        pool_id = await repo.create_pool(InventoryPool(name="测试池", type="data"))

        first = await repo.import_units(pool_id, ["A", "B"])
        second = await repo.import_units(pool_id, ["B", "C", "  "])

        assert first == UnitImportSummary(added=2, skipped_duplicates=0, skipped_blank=0)
        assert second == UnitImportSummary(added=1, skipped_duplicates=1, skipped_blank=1)
        assert await repo.pool_counts(pool_id) == {
            "available": 3,
            "reserved": 0,
            "consumed": 0,
            "uncertain": 0,
            "total": 3,
        }


async def test_reserve_then_release_returns_units_to_the_pool(inventory_db) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        pool_id = await _seed_pool(repo)

        reservation = await repo.reserve(pool_id, "job-1", 2)

        assert reservation.quantity == 2
        assert reservation.state == ReservationState.RESERVED.value
        assert len(reservation.unit_ids) == 2
        assert await repo.pool_counts(pool_id) == {
            "available": 1,
            "reserved": 2,
            "consumed": 0,
            "uncertain": 0,
            "total": 3,
        }
        assert [
            unit.reserved_by for unit in await repo.units_of_reservation(reservation.local_id)
        ] == [
            "job-1",
            "job-1",
        ]

        released = await repo.release(reservation.local_id)

        assert released == 2
        assert (await repo.get_reservation(reservation.local_id)).state == "released"
        assert await repo.pool_counts(pool_id) == {
            "available": 3,
            "reserved": 0,
            "consumed": 0,
            "uncertain": 0,
            "total": 3,
        }


async def test_reserve_then_consume_keeps_units_out_of_stock(inventory_db) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        pool_id = await _seed_pool(repo)
        reservation = await repo.reserve(pool_id, "job-1", 1)

        consumed = await repo.consume(reservation.local_id)

        assert consumed == 1
        units = await repo.units_of_reservation(reservation.local_id)
        assert [unit.state for unit in units] == ["consumed"]
        assert units[0].consumed_at is not None
        assert (await repo.pool_counts(pool_id))["available"] == 2


async def test_insufficient_stock_leaves_the_pool_untouched(inventory_db) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        pool_id = await _seed_pool(repo, units=("CARD-1", "CARD-2"))

        with pytest.raises(InsufficientStock) as excinfo:
            await repo.reserve(pool_id, "job-1", 3)

        assert excinfo.value.pool_id == pool_id
        assert excinfo.value.available == 2
        assert await repo.pool_counts(pool_id) == {
            "available": 2,
            "reserved": 0,
            "consumed": 0,
            "uncertain": 0,
            "total": 2,
        }
        assert await repo.list_reservations(job_ref="job-1") == []


async def test_stale_candidate_aborts_the_whole_reservation(
    inventory_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        pool_id = await _seed_pool(repo)
        await repo.reserve(pool_id, "job-a", 1)
        stale_ids = tuple(unit.local_id for unit in await repo.list_units(pool_id))
        # 模拟"读候选之后、写之前被别的预留抢走":仓储备选视图已过期。
        monkeypatch.setattr(
            inventory_repo,
            "plan_reservation",
            lambda _ids, quantity: stale_ids[:quantity],
        )

    with pytest.raises(ConcurrentReservationError, match="整体回滚"):
        async with db_mod.async_session_factory() as session, session.begin():
            await _repo(session).reserve(pool_id, "job-b", 2)

    async with db_mod.async_session_factory() as session:
        repo = _repo(session)
        assert await repo.pool_counts(pool_id) == {
            "available": 2,
            "reserved": 1,
            "consumed": 0,
            "uncertain": 0,
            "total": 3,
        }
        assert await repo.list_reservations(job_ref="job-b") == []


async def test_uncertain_keeps_units_frozen_until_resolved(inventory_db) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        pool_id = await _seed_pool(repo)
        reservation = await repo.reserve(pool_id, "job-1", 2)

        frozen = await repo.mark_uncertain(reservation.local_id)

        assert frozen == 2
        units = await repo.units_of_reservation(reservation.local_id)
        assert {unit.state for unit in units} == {InventoryUnitState.UNCERTAIN.value}
        assert {unit.reserved_by for unit in units} == {"job-1"}
        assert (await repo.pool_counts(pool_id))["available"] == 1

        resolved = await repo.resolve_uncertain(reservation.local_id, delivered=True)

        assert resolved == 2
        assert await repo.pool_counts(pool_id) == {
            "available": 1,
            "reserved": 0,
            "consumed": 2,
            "uncertain": 0,
            "total": 3,
        }


async def test_uncertain_can_be_released_when_delivery_never_happened(inventory_db) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        pool_id = await _seed_pool(repo)
        reservation = await repo.reserve(pool_id, "job-1", 1)
        await repo.mark_uncertain(reservation.local_id)

        await repo.resolve_uncertain(reservation.local_id, delivered=False)

        assert (await repo.pool_counts(pool_id))["available"] == 3


async def test_pool_guards_reject_invalid_operations(inventory_db) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        repo = _repo(session)
        text_pool = await _seed_pool(repo, units=(), type_="text")

        with pytest.raises(ValueError, match="只有 data 池"):
            await repo.import_units(text_pool, ["A"])
        with pytest.raises(ValueError, match="不按单元预留"):
            await repo.reserve(text_pool, "job-1", 1)

        disabled = await _seed_pool(repo, enabled=False)
        with pytest.raises(ValueError, match="已停用"):
            await repo.reserve(disabled, "job-1", 1)

        pool_id = await _seed_pool(repo)
        active = await repo.reserve(pool_id, "job-1", 1)
        await repo.consume(active.local_id)
        with pytest.raises(InvalidStateTransition, match="不能从 consumed"):
            await repo.release(active.local_id)
        with pytest.raises(ValueError, match="不存在"):
            await repo.release(999999)
