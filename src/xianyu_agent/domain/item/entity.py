"""商品领域模型(Issue #4 Phase 4)。

结构化字段是业务事实,raw_payload 只是协议校准用的辅助材料。local_id 是本地主键,
external_item_id 是平台商品 id 且在同一账号内唯一:两者不能混用,否则跨账号的同名
商品会串在一起。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from .sku import ItemVariant, parse_price, parse_stock, select_available_variant, specs_from_stored


@dataclass(frozen=True)
class Item:
    """One mirrored listing with its structured specs."""

    external_item_id: str
    account_id: str
    title: str
    price: Decimal | None = None
    description: str | None = None
    category_id: str | None = None
    status: str | None = None
    stock: int | None = None
    images: tuple[str, ...] = ()
    variants: tuple[ItemVariant, ...] = ()
    sold_count: int = 0
    has_multi_spec: bool | None = None
    is_on_sale: bool = True
    detail_url: str | None = None
    main_image_url: str | None = None
    listed_at: datetime | None = None
    synced_at: datetime | None = None
    # 商品级人工覆盖:None 由策略判定,EXCLUDE 完全排除,KEEP 强制保留。
    automation_override: str | None = None
    automation_note: str | None = None
    automation_overridden_at: datetime | None = None
    automation_overridden_by: str | None = None
    local_id: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_item_id.strip():
            msg = "external_item_id 不能为空"
            raise ValueError(msg)
        if not self.account_id.strip():
            msg = "account_id 不能为空"
            raise ValueError(msg)
        if not self.title.strip():
            msg = "title 不能为空"
            raise ValueError(msg)
        if self.price is not None and self.price < 0:
            msg = "price 不能为负数"
            raise ValueError(msg)
        if self.stock is not None and self.stock < 0:
            msg = "stock 不能为负数"
            raise ValueError(msg)
        if self.sold_count < 0:
            msg = "sold_count 不能为负数"
            raise ValueError(msg)

    @property
    def has_variants(self) -> bool:
        return bool(self.variants)

    @property
    def images_or_main(self) -> tuple[str, ...]:
        """All known pictures, falling back to the single mirrored main image."""
        if self.images:
            return self.images
        return (self.main_image_url,) if self.main_image_url else ()

    @property
    def effective_price(self) -> Decimal | None:
        """Cheapest sellable price: variant price when specs exist, else list price."""
        if not self.variants:
            return self.price
        variant_prices = [
            variant.price
            for variant in self.variants
            if variant.is_available and variant.price is not None
        ]
        return min(variant_prices) if variant_prices else None

    def variant(self, specs: object) -> ItemVariant | None:
        """Return the variant matching a buyer selection, only when sellable."""
        return select_available_variant(self.variants, specs)  # type: ignore[arg-type]


def from_row(row: Any, *, variants: Sequence[ItemVariant] = ()) -> Item:
    """Build the domain item from a persisted mirror row.

    Missing or dirty mirrored text yields None instead of a guessed value, so an
    unparsable price never becomes a silent 0.
    """
    images = _string_tuple(row.images)
    return Item(
        external_item_id=str(row.item_id),
        account_id=str(row.account_id),
        title=str(row.title),
        price=parse_price(row.price),
        description=_text_or_none(row.description),
        category_id=_text_or_none(row.category_id),
        status=_text_or_none(row.status),
        stock=parse_stock(row.stock),
        images=images,
        variants=tuple(variants),
        sold_count=int(row.sold_count or 0),
        has_multi_spec=row.has_multi_spec,
        is_on_sale=bool(row.is_on_sale),
        detail_url=_text_or_none(row.detail_url),
        main_image_url=_text_or_none(row.main_image_url),
        listed_at=row.first_seen_at,
        synced_at=row.last_synced_at,
        automation_override=_text_or_none(row.automation_override),
        automation_note=_text_or_none(row.automation_note),
        automation_overridden_at=row.automation_overridden_at,
        automation_overridden_by=_text_or_none(row.automation_overridden_by),
        local_id=row.id,
    )


def variant_from_row(row: Any) -> ItemVariant:
    """Build one domain spec unit from a persisted row."""
    return ItemVariant(
        specs=specs_from_stored(row.attributes),
        external_sku_id=_text_or_none(row.external_sku_id),
        price=parse_price(row.price),
        stock=int(row.stock or 0),
        image_url=_text_or_none(row.image_url),
        enabled=bool(row.enabled),
    )


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(text for text in (_text_or_none(entry) for entry in raw) if text is not None)
