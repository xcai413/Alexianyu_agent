"""SKU / 多规格领域模型(M10)。

结构化模型是唯一业务事实,JSON 只承担存储适配。规格用有序的 规格名+规格值 集合
表示;匹配、唯一性与去重都以规范化的规格名为准,因此同一个商品不会因为规格顺序
不同而产生两个 key。

本模块是纯领域逻辑,不做 IO:行情/库存的读取与持久化由 application 层负责。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

PRICE_QUANTUM = Decimal("0.01")
_MAX_SANE_STOCK = 1_000_000


@dataclass(frozen=True)
class SpecValue:
    """One spec dimension of a multi-spec item, e.g. 颜色=红色."""

    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.value.strip():
            msg = "规格名与规格值均不能为空"
            raise ValueError(msg)
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "value", self.value.strip())


SpecInput = Mapping[str, str] | Iterable[tuple[str, str]]


def normalize_specs(raw: SpecInput) -> tuple[SpecValue, ...]:
    """Return specs in declaration order, rejecting duplicate or empty names.

    Declaration order is preserved because listings show specs in the order the
    operator declared them. Identity does not depend on that order: spec_key()
    sorts, so the same combination always has the same key.
    """
    pairs = raw.items() if isinstance(raw, Mapping) else raw
    collected: dict[str, SpecValue] = {}
    for name, value in pairs:
        spec = SpecValue(name=name, value=value)
        if spec.name in collected:
            msg = f"规格名重复: {spec.name}"
            raise ValueError(msg)
        collected[spec.name] = spec
    return tuple(collected.values())


def specs_from_stored(raw: object) -> tuple[SpecValue, ...]:
    """Parse the stored JSON form: a list of {"name": ..., "value": ...} objects."""
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        msg = "规格存储格式必须是列表"
        raise TypeError(msg)
    pairs: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or "name" not in entry or "value" not in entry:
            msg = "规格项必须包含 name 与 value"
            raise ValueError(msg)
        pairs.append((str(entry["name"]), str(entry["value"])))
    return normalize_specs(pairs)


def specs_to_stored(specs: Sequence[SpecValue]) -> list[dict[str, str]]:
    """Render specs for the auxiliary JSON column."""
    return [{"name": spec.name, "value": spec.value} for spec in specs]


def spec_key(specs: Sequence[SpecValue]) -> tuple[tuple[str, str], ...]:
    """Stable identity of one spec combination, independent of input order."""
    return tuple(sorted((spec.name, spec.value) for spec in specs))


def parse_stock(value: object) -> int | None:
    """Parse a stock count from mirrored text; unusable input yields None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        stock = value
    elif isinstance(value, (Decimal, float, str)):
        try:
            stock = int(Decimal(str(value).strip()))
        except (InvalidOperation, ValueError, ArithmeticError):
            return None
    else:
        return None
    if stock < 0 or stock > _MAX_SANE_STOCK:
        return None
    return stock


def parse_price(value: object) -> Decimal | None:
    """Parse a mirrored price string such as "45", "45.5" or "¥45"; None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, (int, float, str)):
        text = str(value).strip()
        text = re.sub(r"^[¥￥]\s*", "", text)
        text = re.sub(r"\s*(元|块钱|块|圆)$", "", text)
        if not text:
            return None
        try:
            amount = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount.quantize(PRICE_QUANTUM)


@dataclass(frozen=True)
class ItemVariant:
    """One purchasable spec combination of an item."""

    specs: tuple[SpecValue, ...]
    external_sku_id: str | None = None
    price: Decimal | None = None
    stock: int = 0
    image_url: str | None = None
    enabled: bool = True
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.specs:
            msg = "多规格单元必须至少包含一个规格"
            raise ValueError(msg)
        if len({spec.name for spec in self.specs}) != len(self.specs):
            msg = "同一多规格单元内的规格名不能重复"
            raise ValueError(msg)
        if self.stock < 0:
            msg = "库存不能为负数"
            raise ValueError(msg)
        if self.stock > _MAX_SANE_STOCK:
            msg = "库存超出合理范围"
            raise ValueError(msg)
        if self.price is not None and self.price < 0:
            msg = "价格不能为负数"
            raise ValueError(msg)

    @property
    def key(self) -> tuple[tuple[str, str], ...]:
        return spec_key(self.specs)

    @property
    def is_available(self) -> bool:
        """Whether this combination may be sold right now."""
        return self.enabled and self.stock > 0

    def matches(self, requested: Mapping[str, str] | Sequence[SpecValue]) -> bool:
        """Whether the requested buyer selection is exactly this combination."""
        wanted = requested if isinstance(requested, Mapping) else _pairs(requested)
        return self.key == spec_key(normalize_specs(wanted))

    def describe(self) -> str:
        """Human-readable spec summary used by templates and logs."""
        return " ".join(f"{spec.name}:{spec.value}" for spec in self.specs)


def _pairs(specs: Sequence[SpecValue]) -> list[tuple[str, str]]:
    return [(spec.name, spec.value) for spec in specs]


def match_variant(
    variants: Sequence[ItemVariant],
    requested: Mapping[str, str] | Sequence[SpecValue],
) -> ItemVariant | None:
    """Return the variant with exactly these specs, ignoring availability."""
    wanted = requested if isinstance(requested, Mapping) else _pairs(requested)
    key = spec_key(normalize_specs(wanted))
    for variant in variants:
        if variant.key == key:
            return variant
    return None


def select_available_variant(
    variants: Sequence[ItemVariant],
    requested: Mapping[str, str] | Sequence[SpecValue],
) -> ItemVariant | None:
    """Return the matching variant only when it is enabled and in stock.

    A sold-out or disabled combination must not silently fall back to another
    spec: the buyer picked a specific one, so no match means no sale.
    """
    variant = match_variant(variants, requested)
    if variant is None or not variant.is_available:
        return None
    return variant


def available_variants(variants: Sequence[ItemVariant]) -> tuple[ItemVariant, ...]:
    return tuple(variant for variant in variants if variant.is_available)


def lowest_available_price(variants: Sequence[ItemVariant]) -> Decimal | None:
    """Cheapest sellable price, used when a listing shows a price range."""
    prices = [
        variant.price for variant in available_variants(variants) if variant.price is not None
    ]
    return min(prices) if prices else None
