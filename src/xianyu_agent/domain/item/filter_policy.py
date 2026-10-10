"""商品筛选规则(Phase 4 商品运营策略)。

纯领域逻辑:给定一个镜像商品,判断它是否落在运营方设定的选品范围内,并给出被排除
的具体原因。原因串只描述规则与阈值,供 Dry Run 说明使用。

价格未知的商品不会因为"没有价格"而被当作在区间内:运营方设了价格区间就说明他
在意价格,此时未知价格应当被排除,而不是蒙一个通过的结论。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .entity import Item


@dataclass(frozen=True)
class ItemFilter:
    """Scope of items an operation may touch."""

    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    category_ids: tuple[str, ...] = ()
    on_sale_only: bool = True

    def __post_init__(self) -> None:
        for value, name in ((self.min_price, "min_price"), (self.max_price, "max_price")):
            if value is not None and value < 0:
                msg = f"{name} 不能为负数"
                raise ValueError(msg)
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            msg = "min_price 不能高于 max_price"
            raise ValueError(msg)
        if any(not keyword.strip() for keyword in (*self.include_keywords, *self.exclude_keywords)):
            msg = "关键词不能为空"
            raise ValueError(msg)

    def reasons(self, item: Item) -> tuple[str, ...]:
        """Reasons this item is out of scope; empty means it matches."""
        problems: list[str] = []
        if self.on_sale_only and not item.is_on_sale:
            problems.append("已非在售")
        haystack = f"{item.title} {item.description or ''}".casefold()
        problems.extend(
            f"命中排除关键词: {keyword}"
            for keyword in self.exclude_keywords
            if keyword.casefold() in haystack
        )
        if self.include_keywords and not any(
            keyword.casefold() in haystack for keyword in self.include_keywords
        ):
            problems.append("未命中任何包含关键词")
        if self.category_ids and (item.category_id or "") not in self.category_ids:
            problems.append("类目不在范围内")
        bounds_set = self.min_price is not None or self.max_price is not None
        if bounds_set and item.price is None:
            problems.append("价格未知")
        elif item.price is not None:
            if self.min_price is not None and item.price < self.min_price:
                problems.append(f"价格低于下限({self.min_price})")
            if self.max_price is not None and item.price > self.max_price:
                problems.append(f"价格高于上限({self.max_price})")
        return tuple(problems)

    def matches(self, item: Item) -> bool:
        return not self.reasons(item)


def select_items(items: Sequence[Item], policy: ItemFilter) -> tuple[Item, ...]:
    """Return the items inside the operator's scope."""
    return tuple(item for item in items if policy.matches(item))
