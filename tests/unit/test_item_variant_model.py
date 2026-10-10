"""Persistence tests for structured multi-spec units (Issue #4 M10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.domain.item.sku import normalize_specs, specs_from_stored, specs_to_stored
from xianyu_agent.infrastructure.database.models.item import Item, ItemVariant
from xianyu_agent.protocol.items_client import RemoteItem


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "variants.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_item(item_id: str = "I-1") -> int:
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
    item = await domain_items.get_item_by_external_id("acc-1", item_id)
    assert item is not None
    return item.id


@pytest.mark.asyncio
async def test_variants_round_trip_with_structured_specs(clean_db) -> None:
    item_pk = await _seed_item()
    specs = normalize_specs({"颜色": "红色", "尺码": "M"})

    async with db_mod.get_async_session() as session:
        session.add(
            ItemVariant(
                item_pk=item_pk,
                external_sku_id="SKU-RED-M",
                attributes=specs_to_stored(specs),
                price="45",
                stock=3,
            )
        )
        await session.commit()

    async with db_mod.get_async_session() as session:
        rows = list((await session.execute(select(ItemVariant))).scalars().all())

    assert len(rows) == 1
    assert rows[0].stock == 3
    assert rows[0].enabled is True
    assert [(spec.name, spec.value) for spec in specs_from_stored(rows[0].attributes)] == [
        ("颜色", "红色"),
        ("尺码", "M"),
    ]


@pytest.mark.asyncio
async def test_external_sku_id_is_unique_per_item(clean_db) -> None:
    item_pk = await _seed_item()

    async def insert_duplicate_sku() -> None:
        async with db_mod.get_async_session() as session:
            session.add(
                ItemVariant(item_pk=item_pk, external_sku_id="SKU-1", attributes=[], stock=1)
            )
            session.add(
                ItemVariant(item_pk=item_pk, external_sku_id="SKU-1", attributes=[], stock=2)
            )
            await session.commit()

    with pytest.raises(IntegrityError):
        await insert_duplicate_sku()


@pytest.mark.asyncio
async def test_deleting_an_item_removes_its_variants(clean_db) -> None:
    item_pk = await _seed_item()
    async with db_mod.get_async_session() as session:
        session.add(ItemVariant(item_pk=item_pk, external_sku_id="SKU-1", attributes=[], stock=1))
        await session.commit()

    async with db_mod.get_async_session() as session:
        item = (await session.execute(select(Item).where(Item.id == item_pk))).scalar_one()
        await session.delete(item)
        await session.commit()

    async with db_mod.get_async_session() as session:
        remaining = list((await session.execute(select(ItemVariant))).scalars().all())

    assert remaining == []
