"""商品详情写入服务:把详情与规格落到本地镜像。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xianyu_agent.domain.item import items as domain_items
from xianyu_agent.domain.item.sku import ItemVariant
from xianyu_agent.protocol.mtop.endpoints.item_detail import (
    ItemDetailFacts,
    XianyuItemDetailClient,
)

from .platform_guard import AccountRiskGate


@dataclass(frozen=True)
class ItemDetailUpdate:
    """Detail fields to persist; None means "leave the mirrored value as is"."""

    description: str | None = None
    images: tuple[str, ...] | None = None
    stock: int | None = None
    detail_url: str | None = None
    variants: tuple[ItemVariant, ...] | None = None


class ItemDetailService:
    """Persist one item's detail payload without touching the protocol layer."""

    def __init__(
        self,
        *,
        detail_client: XianyuItemDetailClient | None = None,
        risk_gate: AccountRiskGate | None = None,
    ) -> None:
        self._detail_client = detail_client
        self._risk_gate = risk_gate or AccountRiskGate()

    async def apply_detail(
        self,
        account_id: str,
        item_id: str,
        update: ItemDetailUpdate,
        *,
        synced_at: datetime | None = None,
    ) -> bool:
        """Write detail fields; spec rows replace platform-sourced specs only."""
        row = await domain_items.get_item_by_external_id(account_id, item_id)
        if row is None:
            return False
        written = await domain_items.set_item_details(
            account_id,
            item_id,
            description=update.description,
            images=update.images,
            stock=update.stock,
            detail_url=update.detail_url,
            synced_at=synced_at,
        )
        if update.variants is not None:
            await domain_items.replace_platform_variants(row.id, update.variants)
        return written

    async def sync_facts(self, account_id: str, item_id: str) -> ItemDetailFacts | None:
        """Fetch the platform detail facts this project can currently name.

        The multi-spec flag and calibrated image URLs are persisted; title and
        description paths remain uncalibrated. A missing item in the
        local mirror returns None instead of creating a partial row.
        """
        if await domain_items.get_item_by_external_id(account_id, item_id) is None:
            return None
        facts = await self.fetch_facts(account_id, item_id)
        await domain_items.set_item_details(
            account_id, item_id, has_multi_spec=facts.multi_spec, images=facts.images
        )
        return facts

    async def fetch_facts(self, account_id: str, item_id: str) -> ItemDetailFacts:
        """只读详情事实,遵守风控闸且不修改本地商品。"""
        client = self._detail_client or XianyuItemDetailClient()
        await self._risk_gate.ensure_ready(account_id)
        try:
            facts = await client.fetch_item_detail(account_id, item_id)
        except Exception as exc:
            await self._risk_gate.record_failure(account_id, exc)
            raise
        return facts

    async def fetch_structure(self, account_id: str, item_id: str) -> Mapping[str, Any]:
        """Fetch the detail response shape behind the same risk gate.

        Calibration runs against the real platform, so it must respect the account
        risk circuit exactly like a normal call; a debug path that skips the gate
        is how an operator ends up hammering a flagged account.
        """
        client = self._detail_client or XianyuItemDetailClient()
        await self._risk_gate.ensure_ready(account_id)
        try:
            return await client.fetch_detail_structure(account_id, item_id)
        except Exception as exc:
            await self._risk_gate.record_failure(account_id, exc)
            raise
