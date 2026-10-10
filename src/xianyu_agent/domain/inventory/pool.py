"""库存池领域模型(Issue #5 Phase 6)。

一个池回答三个问题:发什么(text/data/image/api)、给谁用(账号/商品/规格范围)、什么时候发
(delay_seconds)。池本身不持有卡密内容——data 池的内容全在单元里,这样才能对单条卡密做原子占用。

api 池只保存 api_config_ref(指向配置记录或机密库条目),不保存任何密钥。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .state import CONSUMABLE_POOL_TYPES, POOL_TYPES, InventoryPoolType

MAX_DELAY_SECONDS = 3600


@dataclass(frozen=True)
class UnitImportSummary:
    """Result of one 逐行卡密导入:写出多少、跳过多少、为什么跳过。"""

    added: int
    skipped_duplicates: int
    skipped_blank: int

    @property
    def seen(self) -> int:
        return self.added + self.skipped_duplicates + self.skipped_blank


def plan_unit_import(
    contents: Iterable[str], existing_contents: Iterable[str] = ()
) -> tuple[tuple[str, ...], UnitImportSummary]:
    """Trim, de-duplicate and order the lines to insert.

    同一批里的重复行和池内已有的行都会被跳过:同一条卡密被卖两次比导入慢一点严重得多。
    跳过数量单独返回,便于导入结果对运营可见。
    """
    known = {line.strip() for line in existing_contents if line.strip()}
    accepted: list[str] = []
    duplicates = 0
    blanks = 0
    for raw in contents:
        line = raw.strip()
        if not line:
            blanks += 1
            continue
        if line in known:
            duplicates += 1
            continue
        known.add(line)
        accepted.append(line)
    return tuple(accepted), UnitImportSummary(
        added=len(accepted), skipped_duplicates=duplicates, skipped_blank=blanks
    )


@dataclass(frozen=True)
class InventoryPool:
    """One delivery source definition."""

    name: str
    type: str
    description: str | None = None
    enabled: bool = True
    delay_seconds: int = 0
    account_scope: str | None = None
    item_scope: str | None = None
    sku_scope: str | None = None
    api_config_ref: str | None = None
    content: str | None = None
    image_url: str | None = None
    local_id: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            msg = "库存池名称不能为空"
            raise ValueError(msg)
        if self.type not in POOL_TYPES:
            msg = f"未知库存池类型: {self.type!r},可选 {POOL_TYPES}"
            raise ValueError(msg)
        if not 0 <= self.delay_seconds <= MAX_DELAY_SECONDS:
            msg = f"延迟发货秒数必须在 0 到 {MAX_DELAY_SECONDS} 之间"
            raise ValueError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "库存池 id 必须为正整数"
            raise ValueError(msg)
        if self.type == InventoryPoolType.API.value and not (self.api_config_ref or "").strip():
            msg = "api 类型库存池必须提供 api_config_ref"
            raise ValueError(msg)
        if self.type != InventoryPoolType.API.value and (self.api_config_ref or "").strip():
            msg = f"{self.type} 类型库存池不允许配置 api_config_ref"
            raise ValueError(msg)
        content = (self.content or "").strip()
        image_url = (self.image_url or "").strip()
        literal_types = {InventoryPoolType.TEXT.value, InventoryPoolType.IMAGE.value}
        if self.type not in literal_types and (content or image_url):
            msg = f"{self.type} 类型库存池不允许配置发货文案或图片地址"
            raise ValueError(msg)
        # 停用的池允许先建后填:运营往往先建好池、再补文案,最后才启用。
        # 一旦启用就必须能真的发出内容,否则发货路径会选中一个空池。
        if self.enabled and self.type == InventoryPoolType.TEXT.value and not content:
            msg = "启用的 text 类型库存池必须提供发货文案(--content)"
            raise ValueError(msg)
        if self.enabled and self.type == InventoryPoolType.IMAGE.value and not image_url:
            msg = "启用的 image 类型库存池必须提供图片地址(--image-url)"
            raise ValueError(msg)
        for field_name in (
            "description",
            "account_scope",
            "item_scope",
            "sku_scope",
            "content",
            "image_url",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, value.strip() or None)

    @property
    def is_consumable(self) -> bool:
        """DATA 池按单元消耗;其余类型是策略型池。"""
        return self.type in CONSUMABLE_POOL_TYPES

    @property
    def waits_before_delivery(self) -> bool:
        return self.delay_seconds > 0

    @property
    def is_literal(self) -> bool:
        """text 与 image 是策略型池:内容就在池里,不消耗单元。"""
        return self.type in {"text", "image"}

    def describe(self) -> str:
        """Safe one-line summary for logs and CLI output (never contains secrets)."""
        scope = "/".join(
            part for part in (self.account_scope, self.item_scope, self.sku_scope) if part
        )
        suffix = f" scope={scope}" if scope else ""
        state = "enabled" if self.enabled else "disabled"
        return f"{self.name}(#{self.local_id or '-'} {self.type} {state}{suffix})"
