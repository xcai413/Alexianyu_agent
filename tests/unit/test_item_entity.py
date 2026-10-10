"""Unit tests for the item domain entity and its mirror mapping."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from xianyu_agent.domain.item.entity import Item, from_row, variant_from_row
from xianyu_agent.domain.item.sku import normalize_specs

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


def _row(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": 7,
        "item_id": "I-1",
        "account_id": 3,
        "title": "多规格商品",
        "price": "45",
        "description": "全新未拆",
        "category_id": "123",
        "status": "1",
        "stock": "4",
        "images": ["https://img.example/a.jpg", None],
        "sold_count": 3,
        "has_multi_spec": True,
        "is_on_sale": True,
        "detail_url": "https://example.invalid/item/I-1",
        "main_image_url": "https://img.example/main.jpg",
        "first_seen_at": NOW,
        "last_synced_at": NOW,
        # 商品级自动化的人工覆盖列;默认没有覆盖。
        "automation_override": None,
        "automation_note": None,
        "automation_overridden_at": None,
        "automation_overridden_by": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_from_row_maps_structured_fields() -> None:
    item = from_row(_row())

    assert item.external_item_id == "I-1"
    assert item.local_id == 7
    assert item.price == Decimal("45.00")
    assert item.stock == 4
    assert item.sold_count == 3
    assert item.has_multi_spec is True
    assert item.images == ("https://img.example/a.jpg",)
    assert item.listed_at == NOW
    assert item.synced_at == NOW
    assert item.has_variants is False


def test_from_row_maps_the_automation_override() -> None:
    item = from_row(
        _row(
            automation_override="keep",
            automation_note="主推款",
            automation_overridden_at=NOW,
            automation_overridden_by="ops-1",
        )
    )

    assert item.automation_override == "keep"
    assert item.automation_note == "主推款"
    assert item.automation_overridden_at == NOW
    assert item.automation_overridden_by == "ops-1"


def test_dirty_mirrored_text_is_not_guessed() -> None:
    item = from_row(_row(price="abc", stock="many", images=None, description="   "))

    assert item.price is None
    assert item.stock is None
    assert item.images == ()
    assert item.description is None
    assert item.images_or_main == ("https://img.example/main.jpg",)


def test_entity_rejects_impossible_state() -> None:
    with pytest.raises(ValueError, match="external_item_id"):
        Item(external_item_id=" ", account_id="acc-1", title="t")
    with pytest.raises(ValueError, match="title"):
        Item(external_item_id="I-1", account_id="acc-1", title="")
    with pytest.raises(ValueError, match="price"):
        Item(external_item_id="I-1", account_id="acc-1", title="t", price=Decimal("-1"))
    with pytest.raises(ValueError, match="sold_count"):
        Item(external_item_id="I-1", account_id="acc-1", title="t", sold_count=-1)


def test_effective_price_and_variant_selection_use_sellable_specs() -> None:
    item = from_row(
        _row(),
        variants=(
            variant_from_row(
                SimpleNamespace(
                    attributes=[{"name": "颜色", "value": "红色"}],
                    external_sku_id="SKU-RED",
                    price="42",
                    stock=2,
                    image_url=None,
                    enabled=True,
                )
            ),
            variant_from_row(
                SimpleNamespace(
                    attributes=[{"name": "颜色", "value": "蓝色"}],
                    external_sku_id="SKU-BLUE",
                    price="39",
                    stock=0,
                    image_url=None,
                    enabled=True,
                )
            ),
        ),
    )

    assert item.has_variants is True
    assert item.effective_price == Decimal("42.00")
    assert item.variant({"颜色": "红色"}) is not None
    assert item.variant({"颜色": "蓝色"}) is None
    assert item.variants[0].key == item.variant(normalize_specs({"颜色": "红色"})).key
