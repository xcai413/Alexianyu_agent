"""库存应用层:池维护、导入、预留与结果未知处理(真实 SQLite)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryQueryService,
    InventoryReservationService,
    InventoryService,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain.inventory import InvalidStateTransition


@pytest.fixture
async def inventory_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "inventory_app.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _data_pool(units: tuple[str, ...] = ("CARD-1", "CARD-2")) -> int:
    pool = await InventoryService().create_pool(name="卡密池", type_="data")
    await InventoryImportService().import_units(pool.local_id, list(units))
    return pool.local_id


async def test_create_pool_validates_and_persists() -> None:
    with pytest.raises(ValueError, match="未知库存池类型"):
        await InventoryService().create_pool(name="池", type_="magic")
    with pytest.raises(ValueError, match="延迟发货秒数"):
        await InventoryService().create_pool(name="池", type_="data", delay_seconds=7200)
    with pytest.raises(ValueError, match="api_config_ref"):
        await InventoryService().create_pool(name="池", type_="api")


async def test_create_and_update_pool_revalidates(inventory_db) -> None:
    service = InventoryService()
    pool = await service.create_pool(name="卡密池", type_="data", delay_seconds=30)

    assert pool.local_id is not None
    assert pool.delay_seconds == 30

    updated = await service.update_pool(pool.local_id, delay_seconds=0, name="卡密池A")

    assert updated.delay_seconds == 0
    assert updated.name == "卡密池A"
    with pytest.raises(ValueError, match="延迟发货秒数"):
        await service.update_pool(pool.local_id, delay_seconds=99999)


async def test_update_pool_can_clear_scope(inventory_db) -> None:
    service = InventoryService()
    pool = await service.create_pool(
        name="范围池", type_="data", account_scope="account-a", item_scope="item-a"
    )

    updated = await service.update_pool(pool.local_id, name="范围池-新", clear_scope=True)

    assert updated.name == "范围池-新"
    assert updated.account_scope is None
    assert updated.item_scope is None


async def test_update_pool_can_replace_scope(inventory_db) -> None:
    service = InventoryService()
    pool = await service.create_pool(name="范围池", type_="data")

    updated = await service.update_pool(
        pool.local_id,
        account_scope="account-b",
        item_scope="item-b",
        sku_scope="颜色=蓝色",
    )

    assert (updated.account_scope, updated.item_scope, updated.sku_scope) == (
        "account-b",
        "item-b",
        "颜色=蓝色",
    )


async def test_import_from_content_and_file(inventory_db) -> None:
    pool = await InventoryService().create_pool(name="卡密池", type_="data")
    service = InventoryImportService()

    summary = await service.import_units(pool.local_id, ["A", "B", "", "A"])
    assert (summary.added, summary.skipped_duplicates, summary.skipped_blank) == (2, 1, 1)

    path = inventory_db / "codes.txt"
    path.write_text("B\nC\n", encoding="utf-8")
    file_summary = await service.import_file(pool.local_id, path)

    assert (file_summary.added, file_summary.skipped_duplicates) == (1, 1)
    view = await InventoryQueryService().pool_detail(pool.local_id)
    assert view.total == 3
    assert view.available == 3


async def test_import_file_rejects_missing_file(inventory_db) -> None:
    pool = await InventoryService().create_pool(name="卡密池", type_="data")

    with pytest.raises(ValueError, match="导入文件不存在"):
        await InventoryImportService().import_file(pool.local_id, inventory_db / "nope.txt")


async def test_reserve_release_and_consume_flow(inventory_db) -> None:
    pool_id = await _data_pool()
    reservations = InventoryReservationService()
    queries = InventoryQueryService()

    reservation = await reservations.reserve(pool_id, "job-1", 2)

    assert reservation.local_id is not None
    assert await reservations.contents(reservation.local_id) == ("CARD-1", "CARD-2")
    assert (await queries.pool_detail(pool_id)).reserved == 2

    assert await reservations.release(reservation.local_id) == 2
    assert (await queries.pool_detail(pool_id)).available == 2

    again = await reservations.reserve(pool_id, "job-2", 1)
    assert await reservations.consume(again.local_id) == 1
    assert (await queries.pool_detail(pool_id)).available == 1


async def test_uncertain_then_resolved_by_hand(inventory_db) -> None:
    pool_id = await _data_pool()
    reservations = InventoryReservationService()

    reservation = await reservations.reserve(pool_id, "job-1", 1)
    assert await reservations.mark_uncertain(reservation.local_id) == 1

    frozen = await InventoryQueryService().pool_detail(pool_id)
    assert frozen.uncertain == 1
    assert frozen.available == 1

    assert await reservations.resolve_uncertain(reservation.local_id, delivered=False) == 1
    assert (await InventoryQueryService().pool_detail(pool_id)).available == 2
    with pytest.raises(InvalidStateTransition):
        await reservations.release(reservation.local_id)


async def test_units_are_masked_in_queries(inventory_db) -> None:
    pool_id = await _data_pool(units=("SECRET-CODE-1",))

    units = await InventoryQueryService().list_units(pool_id)

    assert units[0].preview == "SE***-1"
    assert "SECRET-CODE-1" not in units[0].preview
    assert await InventoryQueryService().plain_contents(pool_id) == ("SECRET-CODE-1",)


async def test_delete_pool_removes_units(inventory_db) -> None:
    pool_id = await _data_pool()
    service = InventoryService()

    await service.delete_pool(pool_id)

    with pytest.raises(ValueError, match="不存在"):
        await InventoryQueryService().pool_detail(pool_id)
