"""表头映射与行转草稿的共享逻辑。

导入只做本地可以证明的规范化:表头按别名匹配,金额与数量走领域解析函数,
缺标题的行无法构成草稿,单独记为被拒绝的行,而不是塞一个空标题进批次。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from xianyu_agent.domain.item.publish_job import PublishDraft
from xianyu_agent.domain.item.sku import parse_price, parse_stock

DEFAULT_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("标题", "商品标题", "title"),
    "price": ("价格", "售价", "price"),
    "description": ("描述", "商品描述", "详情", "description"),
    "category_id": ("类目", "类目id", "category", "category_id"),
    "images": ("图片", "图片链接", "主图", "images"),
    "quantity": ("数量", "库存", "quantity", "stock"),
    "original_price": ("原价", "original_price"),
    "postage_mode": ("运费模式", "postage_mode"),
    "postage": ("运费", "postage"),
}
TITLE_FIELD = "title"
IMAGE_SEPARATORS = re.compile(r"[,，;；|\n]+")  # noqa: RUF001 - 全角分隔符是真实输入


@dataclass(frozen=True)
class RejectedRow:
    """A row that cannot become a draft, kept so the operator sees where it failed."""

    row_number: int
    reason: str


@dataclass(frozen=True)
class ImportResult:
    """Normalized drafts plus what was ignored while reading the file."""

    drafts: tuple[PublishDraft, ...]
    rejected: tuple[RejectedRow, ...] = ()
    ignored_headers: tuple[str, ...] = ()
    bundled_images: tuple[str, ...] = field(default=())


def rows_to_drafts(
    rows: Sequence[Sequence[str]],
    *,
    account_id: str,
    aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> ImportResult:
    """Turn a header row plus data rows into drafts."""
    if not rows:
        return ImportResult(drafts=())
    header = [cell.strip() for cell in rows[0]]
    mapping = _header_mapping(header, aliases or DEFAULT_HEADER_ALIASES)
    ignored = tuple(
        name for index, name in enumerate(header) if name and index not in mapping.values()
    )
    drafts: list[PublishDraft] = []
    rejected: list[RejectedRow] = []
    for offset, row in enumerate(rows[1:], start=2):
        values = _row_values(row, mapping)
        if not any(value.strip() for value in values.values()):
            continue
        title = values.get(TITLE_FIELD, "").strip()
        if not title:
            rejected.append(RejectedRow(row_number=offset, reason="缺少标题"))
            continue
        drafts.append(_build_draft(title, values, account_id=account_id, row_number=offset))
    return ImportResult(drafts=tuple(drafts), rejected=tuple(rejected), ignored_headers=ignored)


def _header_mapping(
    header: Sequence[str], aliases: Mapping[str, tuple[str, ...]]
) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for field_name, names in aliases.items():
        wanted = {name.casefold() for name in names}
        for index, cell in enumerate(header):
            if cell.casefold() in wanted and index not in mapping.values():
                mapping[field_name] = index
                break
    return mapping


def _row_values(row: Sequence[str], mapping: Mapping[str, int]) -> dict[str, str]:
    return {
        field_name: str(row[index]).strip() if index < len(row) else ""
        for field_name, index in mapping.items()
    }


def _build_draft(
    title: str, values: Mapping[str, str], *, account_id: str, row_number: int
) -> PublishDraft:
    quantity = parse_stock(values.get("quantity")) or 1
    postage_mode = (values.get("postage_mode") or "").strip().lower()
    return PublishDraft(
        account_id=account_id,
        title=title,
        price=parse_price(values.get("price")),
        description=values.get("description") or None,
        category_id=values.get("category_id") or None,
        images=_split_images(values.get("images", "")),
        quantity=max(quantity, 1),
        original_price=parse_price(values.get("original_price")),
        postage_mode="fixed" if postage_mode in {"fixed", "固定", "买家付"} else "free",
        postage=parse_price(values.get("postage")),
        source_row=row_number,
        source=dict(values),
    )


def _split_images(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in IMAGE_SEPARATORS.split(raw) if part.strip())
