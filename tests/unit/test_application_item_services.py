"""Application-layer tests for the item mirror, specs and detail writes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.application.item import (
    ItemDetailService,
    ItemDetailUpdate,
    ItemQueryService,
    ItemSyncService,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.domain.item.sku import ItemVariant as DomainVariant, normalize_specs
from xianyu_agent.infrastructure.database.models.item import ItemVariant
from xianyu_agent.infrastructure.database.models.order import Order
from xianyu_agent.protocol.items_client import ItemSnapshot, RemoteItem
from xianyu_agent.protocol.mtop.endpoints.item_detail import ItemDetailFacts

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "item_app.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _remote(item_id: str, title: str = "多规格商品", price: str = "45") -> RemoteItem:
    return RemoteItem(
        item_id=item_id,
        title=title,
        price=price,
        status="1",
        detail_url=f"https://example.invalid/item/{item_id}",
        main_image_url=f"https://img.example/{item_id}.jpg",
        category_id="123",
        auction_type="a",
        raw_payload={"id": item_id},
    )


class FakeItemsClient:
    def __init__(self, snapshot: ItemSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, int, int]] = []

    async def fetch_all_on_sale(
        self, account_id: str, *, page_size: int, max_pages: int
    ) -> ItemSnapshot:
        self.calls.append((account_id, page_size, max_pages))
        return self.snapshot


async def _seed_order(item_id: str, *, quantity: int, status: str = "paid") -> None:
    async with db_mod.get_async_session() as session:
        account = (await session.execute(select(Account).limit(1))).scalar_one()
        session.add(
            Order(
                account_id=account.id,
                order_id=f"O-{item_id}-{status}-{quantity}",
                item_id=item_id,
                amount=45.0,
                quantity=quantity,
                status=status,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_sync_service_replaces_mirror_and_refreshes_sold_counts(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_items.apply_on_sale_snapshot("acc-1", [_remote("I-OLD")], synced_at=NOW)
    client = FakeItemsClient(
        ItemSnapshot(items=[_remote("I-1"), _remote("I-2", price="39")], fetched_at=NOW, pages=2)
    )
    service = ItemSyncService(lambda: client)

    outcome = await service.sync_on_sale("acc-1", page_size=20, max_pages=5)

    assert client.calls == [("acc-1", 20, 5)]
    assert outcome.pages == 2
    assert outcome.result.created == 2
    assert outcome.result.marked_off_sale == 1
    assert outcome.sold_counts_updated == 0

    await _seed_order("I-1", quantity=3)
    await _seed_order("I-1", quantity=1, status="cancelled")
    refreshed = await service.sync_on_sale("acc-1")
    listed = {item.external_item_id: item for item in await ItemQueryService().list_items("acc-1")}

    assert refreshed.sold_counts_updated == 1
    assert listed["I-1"].sold_count == 3
    assert listed["I-2"].sold_count == 0
    assert listed["I-1"].price == Decimal("45")


@pytest.mark.asyncio
async def test_query_service_reads_specs_with_the_detail(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_items.apply_on_sale_snapshot("acc-1", [_remote("I-1")], synced_at=NOW)
    row = await domain_items.get_item_by_external_id("acc-1", "I-1")
    assert row is not None
    await domain_items.replace_platform_variants(
        row.id,
        [
            DomainVariant(
                specs=normalize_specs({"颜色": "红色", "尺码": "M"}),
                external_sku_id="SKU-RED-M",
                price=Decimal("45.00"),
                stock=2,
            ),
            DomainVariant(
                specs=normalize_specs({"颜色": "蓝色", "尺码": "M"}),
                external_sku_id="SKU-BLUE-M",
                price=Decimal("39.00"),
                stock=0,
            ),
        ],
    )

    item = await ItemQueryService().get_item("acc-1", "I-1")

    assert item is not None
    assert item.has_variants is True
    assert [variant.external_sku_id for variant in item.variants] == ["SKU-RED-M", "SKU-BLUE-M"]
    assert item.effective_price == Decimal("45.00")
    assert item.variant({"颜色": "蓝色", "尺码": "M"}) is None


@pytest.mark.asyncio
async def test_detail_service_writes_fields_and_keeps_operator_specs(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_items.apply_on_sale_snapshot("acc-1", [_remote("I-1")], synced_at=NOW)
    row = await domain_items.get_item_by_external_id("acc-1", "I-1")
    assert row is not None
    async with db_mod.get_async_session() as session:
        session.add(
            ItemVariant(
                item_pk=row.id,
                external_sku_id=None,
                attributes=[{"name": "颜色", "value": "定制"}],
                price="50",
                stock=1,
            )
        )
        await session.commit()
    await domain_items.replace_platform_variants(
        row.id,
        [
            DomainVariant(
                specs=normalize_specs({"颜色": "红色"}),
                external_sku_id="SKU-STALE",
                price=Decimal("45.00"),
                stock=1,
            )
        ],
    )
    service = ItemDetailService()

    written = await service.apply_detail(
        "acc-1",
        "I-1",
        ItemDetailUpdate(
            description="全新未拆",
            images=("https://img.example/a.jpg", "https://img.example/b.jpg"),
            stock=4,
            variants=(
                DomainVariant(
                    specs=normalize_specs({"颜色": "红色"}),
                    external_sku_id="SKU-RED",
                    price=Decimal("42.00"),
                    stock=3,
                ),
            ),
        ),
        synced_at=NOW,
    )

    assert written is True
    item = await ItemQueryService().get_item("acc-1", "I-1")
    assert item is not None
    assert item.description == "全新未拆"
    assert item.images == ("https://img.example/a.jpg", "https://img.example/b.jpg")
    assert item.stock == 4
    assert sorted(variant.external_sku_id or "operator" for variant in item.variants) == [
        "SKU-RED",
        "operator",
    ]


@pytest.mark.asyncio
async def test_detail_service_ignores_unknown_items_and_partial_sold_counts(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_items.apply_on_sale_snapshot(
        "acc-1", [_remote("I-1"), _remote("I-2")], synced_at=NOW
    )

    written = await ItemDetailService().apply_detail(
        "acc-1", "I-MISSING", ItemDetailUpdate(description="x")
    )
    touched = await domain_items.apply_sold_counts("acc-1", {"I-1": 2})
    listed = {item.external_item_id: item for item in await ItemQueryService().list_items("acc-1")}

    assert written is False
    assert touched == 1
    assert listed["I-1"].sold_count == 2
    assert listed["I-2"].sold_count == 0


@pytest.mark.asyncio
async def test_detail_sync_stores_only_the_facts_we_can_name(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_items.apply_on_sale_snapshot("acc-1", [_remote("I-1")], synced_at=NOW)
    before = await ItemQueryService().get_item("acc-1", "I-1")
    assert before is not None
    assert before.has_multi_spec is None

    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def fetch_item_detail(self, account_id: str, item_id: str) -> ItemDetailFacts:
            self.calls.append((account_id, item_id))
            return ItemDetailFacts(
                item_id=item_id,
                publisher_id="777",
                multi_spec=True,
                images=("https://img.example/one.png", "https://img.example/two.png"),
            )

    client = _Client()
    service = ItemDetailService(detail_client=client)  # type: ignore[arg-type]

    facts = await service.sync_facts("acc-1", "I-1")
    after = await ItemQueryService().get_item("acc-1", "I-1")
    missing = await service.sync_facts("acc-1", "I-MISSING")

    assert facts is not None
    assert facts.publisher_id == "777"
    assert client.calls == [("acc-1", "I-1")]
    assert after is not None
    assert after.has_multi_spec is True
    assert after.images == ("https://img.example/one.png", "https://img.example/two.png")
    assert after.title == "多规格商品"
    assert missing is None
