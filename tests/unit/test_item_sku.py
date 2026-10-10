"""Unit tests for the SKU / multi-spec domain model (Issue #4 M10)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from xianyu_agent.domain.item.sku import (
    ItemVariant,
    SpecValue,
    available_variants,
    lowest_available_price,
    match_variant,
    normalize_specs,
    parse_price,
    parse_stock,
    select_available_variant,
    spec_key,
    specs_from_stored,
    specs_to_stored,
)

COLOR = "颜色"
SIZE = "尺码"


def _variant(
    color: str = "红色",
    size: str = "M",
    *,
    sku_id: str | None = "SKU-1",
    price: str = "45",
    stock: int = 3,
    enabled: bool = True,
) -> ItemVariant:
    return ItemVariant(
        specs=normalize_specs({COLOR: color, SIZE: size}),
        external_sku_id=sku_id,
        price=Decimal(price),
        stock=stock,
        enabled=enabled,
    )


def test_normalize_specs_keeps_declaration_order_and_rejects_duplicates() -> None:
    specs = normalize_specs({SIZE: "M ", COLOR: " 红色"})

    assert [spec.name for spec in specs] == [SIZE, COLOR]
    assert specs[0] == SpecValue(name=SIZE, value="M")
    assert specs[1] == SpecValue(name=COLOR, value="红色")
    with pytest.raises(ValueError, match="重复"):
        normalize_specs([(COLOR, "红色"), (COLOR, "蓝色")])
    with pytest.raises(ValueError, match="不能为空"):
        normalize_specs({COLOR: "   "})


def test_spec_key_is_independent_of_input_order() -> None:
    first = normalize_specs([(COLOR, "红色"), (SIZE, "M")])
    second = normalize_specs([(SIZE, "M"), (COLOR, "红色")])

    assert spec_key(first) == spec_key(second)
    assert _variant().key == spec_key(second)


def test_variant_validation_rejects_impossible_state() -> None:
    with pytest.raises(ValueError, match="至少包含一个规格"):
        ItemVariant(specs=())
    with pytest.raises(ValueError, match="规格名不能重复"):
        ItemVariant(specs=(SpecValue(COLOR, "红色"), SpecValue(COLOR, "蓝色")))
    with pytest.raises(ValueError, match="库存不能为负数"):
        ItemVariant(specs=(SpecValue(COLOR, "红色"),), stock=-1)
    with pytest.raises(ValueError, match="价格不能为负数"):
        ItemVariant(specs=(SpecValue(COLOR, "红色"),), price=Decimal("-1"))


def test_variant_availability_requires_enabled_and_stock() -> None:
    assert _variant().is_available is True
    assert _variant(stock=0).is_available is False
    assert _variant(enabled=False).is_available is False


def test_match_variant_accepts_a_mapping_or_spec_sequence() -> None:
    variants = [_variant(), _variant(color="蓝色")]

    assert match_variant(variants, {COLOR: "红色", SIZE: "M"}) is variants[0]
    assert match_variant(variants, [SpecValue(SIZE, "M"), SpecValue(COLOR, "蓝色")]) is variants[1]
    assert match_variant(variants, {COLOR: "绿色", SIZE: "M"}) is None
    assert match_variant(variants, {COLOR: "红色"}) is None


def test_sold_out_or_disabled_spec_is_not_substituted() -> None:
    variants = [_variant(stock=0), _variant(color="蓝色")]
    requested = {COLOR: "红色", SIZE: "M"}

    assert match_variant(variants, requested) is variants[0]
    assert select_available_variant(variants, requested) is None
    assert select_available_variant([_variant(enabled=False)], requested) is None
    assert select_available_variant(variants, {COLOR: "蓝色", SIZE: "M"}) is variants[1]


def test_available_variants_and_lowest_price() -> None:
    variants = [
        _variant(price="45", stock=2),
        _variant(color="蓝色", price="39", stock=1),
        _variant(color="绿色", price="35", stock=0),
        _variant(color="黄色", price="29", enabled=False),
    ]

    assert [variant.describe() for variant in available_variants(variants)] == [
        "颜色:红色 尺码:M",
        "颜色:蓝色 尺码:M",
    ]
    assert lowest_available_price(variants) == Decimal("39")
    assert lowest_available_price([_variant(stock=0)]) is None


def test_variant_describe_uses_sorted_spec_names() -> None:
    assert _variant().describe() == "颜色:红色 尺码:M"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("45", Decimal("45.00")),
        (" 45.5 ", Decimal("45.50")),
        ("¥45", Decimal("45.00")),
        ("45元", Decimal("45.00")),
        ("0", Decimal("0.00")),
        (Decimal("39.9"), Decimal("39.90")),
        (45, Decimal("45.00")),
        ("", None),
        (None, None),
        ("abc", None),
        ("-5", None),
        (True, None),
        (float("nan"), None),
    ],
)
def test_parse_price_reads_mirrored_price_text(raw: object, expected: Decimal | None) -> None:
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", 12),
        (3, 3),
        (Decimal("7"), 7),
        ("0", 0),
        ("", None),
        (None, None),
        ("abc", None),
        ("-1", None),
        (True, None),
        (10**9, None),
    ],
)
def test_parse_stock_reads_mirrored_stock_text(raw: object, expected: int | None) -> None:
    assert parse_stock(raw) == expected


def test_specs_round_trip_through_the_auxiliary_json_form() -> None:
    specs = normalize_specs({COLOR: "红色", SIZE: "M"})

    stored = specs_to_stored(specs)

    assert stored == [{"name": COLOR, "value": "红色"}, {"name": SIZE, "value": "M"}]
    assert specs_from_stored(stored) == specs
    assert specs_from_stored(None) == ()
    assert specs_from_stored([]) == ()
    with pytest.raises(TypeError, match="必须是列表"):
        specs_from_stored({"颜色": "红色"})
    with pytest.raises(ValueError, match="name 与 value"):
        specs_from_stored([{"color": "red"}])
