"""商品同步服务:在售快照与已售件数。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from xianyu_agent.domain.item import items as domain_items
from xianyu_agent.domain.item.items import ItemSyncResult
from xianyu_agent.domain.order import orders as domain_orders
from xianyu_agent.protocol.items_client import DEFAULT_PAGE_SIZE, XianyuItemsClient

from .platform_guard import AccountRiskGate

ClientFactory = Callable[[], XianyuItemsClient]
DEFAULT_MAX_PAGES = 100


@dataclass(frozen=True)
class ItemSyncOutcome:
    """Result of one on-sale mirror sync."""

    result: ItemSyncResult
    pages: int
    sold_counts_updated: int


class ItemSyncService:
    """Own the on-sale mirror sync; adapters only call this."""

    def __init__(
        self,
        client_factory: ClientFactory | None = None,
        *,
        risk_gate: AccountRiskGate | None = None,
    ) -> None:
        self._client_factory = client_factory or XianyuItemsClient
        self._risk_gate = risk_gate or AccountRiskGate()

    async def sync_on_sale(
        self,
        account_id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        refresh_sold_counts: bool = True,
    ) -> ItemSyncOutcome:
        """Replace the mirror from a complete snapshot, then refresh sold counts.

        Sold counts come from aggregated orders, never from the listing payload,
        so a cancelled order cannot be reported as a sale.
        """
        await self._risk_gate.ensure_ready(account_id)
        try:
            snapshot = await self._client_factory().fetch_all_on_sale(
                account_id, page_size=page_size, max_pages=max_pages
            )
        except Exception as exc:
            await self._risk_gate.record_failure(account_id, exc)
            raise
        result = await domain_items.apply_on_sale_snapshot(
            account_id, snapshot.items, synced_at=snapshot.fetched_at
        )
        updated = 0
        if refresh_sold_counts:
            summaries = await domain_orders.sales_by_item(account_id)
            updated = await domain_items.apply_sold_counts(
                account_id,
                {item_id: summary.sold_quantity for item_id, summary in summaries.items()},
            )
        return ItemSyncOutcome(result=result, pages=snapshot.pages, sold_counts_updated=updated)
