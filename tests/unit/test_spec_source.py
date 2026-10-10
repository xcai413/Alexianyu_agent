"""多规格发货:规格组合 -> 库存池 -> 原子预留(Issue #5 Phase 6, 6C)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.application.delivery import SpecSourceService
from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryQueryService,
    InventoryReservationService,
    InventoryService,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.domain.inventory import InvalidStateTransition
from xianyu_agent.domain.inventory.reservation import InsufficientStock
from xianyu_agent.domain.item.sku import normalize_specs
from xianyu_agent.domain.item.sku_binding import (
    CARD_TARGET,
    POOL_TARGET,
    SpecBinding,
    upsert_item_binding,
)
from xianyu_agent.protocol.items_client import RemoteItem

RED_M = {"颜色": "红色", "尺码": "M"}
BLUE_M = {"颜色": "蓝色", "尺码": "M"}


@pytest.fixture
async def spec_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "spec_source.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_item(item_id: str = "I-1") -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id=item_id,
                title="多规格商品",
                price="45",
                status="1",
                detail_url=None,
                main_image_url=None,
                category_id=None,
                auction_type=None,
                raw_payload=None,
            )
        ],
    )


async def _seed_pool(units: tuple[str, ...], *, name: str = "卡密池") -> int:
    pool = await InventoryService().create_pool(name=name, type_="data")
    await InventoryImportService().import_units(pool.local_id, list(units))
    return pool.local_id


async def _bind(selection: dict[str, str], *, kind: str, target: int, quantity: int = 1) -> None:
    await upsert_item_binding(
        "acc-1",
        "I-1",
        SpecBinding(
            specs=normalize_specs(selection),
            target_kind=kind,
            target_id=target,
            quantity=quantity,
        ),
    )


async def test_plan_reports_missing_item(spec_db) -> None:
    plan = await SpecSourceService().plan("acc-1", "I-404", RED_M)

    assert plan.matched is False
    assert "不在本地镜像" in plan.reason


async def test_plan_reports_unconfigured_selection(spec_db) -> None:
    await _seed_item()
    pool_id = await _seed_pool(("CARD-1",))
    await _bind(RED_M, kind=POOL_TARGET, target=pool_id)

    plan = await SpecSourceService().plan("acc-1", "I-1", BLUE_M)

    assert plan.matched is False
    assert "未配置" in plan.reason


async def test_plan_describes_the_bound_pool(spec_db) -> None:
    await _seed_item()
    pool_id = await _seed_pool(("CARD-1", "CARD-2"))
    await _bind(RED_M, kind=POOL_TARGET, target=pool_id, quantity=2)

    plan = await SpecSourceService().plan("acc-1", "I-1", RED_M)

    assert plan.matched is True
    assert plan.target_kind == POOL_TARGET
    assert plan.target_id == pool_id
    assert plan.quantity == 2
    assert plan.describe() == f"pool:{pool_id} x2"


async def test_reserve_takes_the_bound_quantity_from_the_pool(spec_db) -> None:
    await _seed_item()
    pool_id = await _seed_pool(("CARD-1", "CARD-2", "CARD-3"))
    await _bind(RED_M, kind=POOL_TARGET, target=pool_id, quantity=2)

    reservation = await SpecSourceService().reserve("acc-1", "I-1", RED_M, job_ref="order-1")

    assert reservation.quantity == 2
    assert reservation.pool_id == pool_id
    view = await InventoryQueryService().pool_detail(pool_id)
    assert view.available == 1
    assert view.reserved == 2


async def test_reserve_refuses_when_stock_is_short(spec_db) -> None:
    await _seed_item()
    pool_id = await _seed_pool(("CARD-1",))
    await _bind(RED_M, kind=POOL_TARGET, target=pool_id, quantity=2)

    with pytest.raises(InsufficientStock):
        await SpecSourceService().reserve("acc-1", "I-1", RED_M, job_ref="order-1")

    view = await InventoryQueryService().pool_detail(pool_id)
    assert view.available == 1
    assert view.reserved == 0


async def test_reserve_refuses_legacy_card_bindings(spec_db) -> None:
    await _seed_item()
    await _bind(RED_M, kind=CARD_TARGET, target=7, quantity=1)

    with pytest.raises(ValueError, match="只支持库存池绑定"):
        await SpecSourceService().reserve("acc-1", "I-1", RED_M, job_ref="order-1")


async def test_disabled_binding_does_not_reserve(spec_db) -> None:
    await _seed_item()
    pool_id = await _seed_pool(("CARD-1",))
    await upsert_item_binding(
        "acc-1",
        "I-1",
        SpecBinding(
            specs=normalize_specs(RED_M),
            target_kind=POOL_TARGET,
            target_id=pool_id,
            enabled=False,
        ),
    )

    plan = await SpecSourceService().plan("acc-1", "I-1", RED_M)

    assert plan.matched is False
    assert "停用" in plan.reason
    with pytest.raises(ValueError, match="停用"):
        await SpecSourceService().reserve("acc-1", "I-1", RED_M, job_ref="order-1")


async def test_spec_selection_must_match_exactly(spec_db) -> None:
    await _seed_item()
    pool_id = await _seed_pool(("CARD-1",))
    await _bind({"颜色": "红色"}, kind=POOL_TARGET, target=pool_id)

    assert (await SpecSourceService().plan("acc-1", "I-1", {"颜色": "红色"})).matched is True
    assert (await SpecSourceService().plan("acc-1", "I-1", RED_M)).matched is False


async def test_reservation_of_a_spec_can_be_released(spec_db) -> None:
    await _seed_item()
    pool_id = await _seed_pool(("CARD-1",))
    await _bind(RED_M, kind=POOL_TARGET, target=pool_id)

    reservation = await SpecSourceService().reserve("acc-1", "I-1", RED_M, job_ref="order-1")
    released = await InventoryReservationService().release(reservation.local_id)

    assert released == 1
    assert (await InventoryQueryService().pool_detail(pool_id)).available == 1
    with pytest.raises(InvalidStateTransition):
        await InventoryReservationService().release(reservation.local_id)
