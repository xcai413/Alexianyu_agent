"""商品查询服务:只读本地镜像,不触碰协议层。"""

from __future__ import annotations

from xianyu_agent.domain.item import items as domain_items
from xianyu_agent.domain.item.entity import Item, from_row, variant_from_row


class ItemQueryService:
    """Read mirrored listings, optionally with their spec units."""

    async def list_items(
        self,
        account_id: str,
        *,
        on_sale_only: bool = True,
        limit: int = 100,
    ) -> tuple[Item, ...]:
        """List mirror rows; specs are not loaded for a bounded list read."""
        rows = await domain_items.list_items(account_id, on_sale_only=on_sale_only, limit=limit)
        return tuple(from_row(row) for row in rows)

    async def get_item(self, account_id: str, item_id: str) -> Item | None:
        """Read one item together with its structured specs."""
        row = await domain_items.get_item_by_external_id(account_id, item_id)
        return await self._with_variants(row)

    async def get_item_by_local_id(self, item_pk: int) -> Item | None:
        """Read one item by local primary key, including its spec units."""
        return await self._with_variants(await domain_items.get_item(item_pk))

    async def _with_variants(self, row: object | None) -> Item | None:
        if row is None:
            return None
        variant_rows = await domain_items.list_variants(row.id)  # type: ignore[attr-defined]
        variants = tuple(variant_from_row(variant_row) for variant_row in variant_rows)
        return from_row(row, variants=variants)  # type: ignore[arg-type]
