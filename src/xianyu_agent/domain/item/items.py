"""账号在售商品本地镜像的领域逻辑。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update

from xianyu_agent.db import Account, Item, ItemVariant, get_async_session
from xianyu_agent.domain.item.entity import Item as DomainItem, from_row
from xianyu_agent.domain.item.lifecycle_policy import (
    OVERRIDE_KEEP,
    OVERRIDE_MODES,
)
from xianyu_agent.domain.item.sku import ItemVariant as DomainVariant, specs_to_stored
from xianyu_agent.protocol.items_client import RemoteItem


@dataclass(frozen=True)
class ItemSyncResult:
    """一次完整在售快照入库的结果。"""

    account_id: str
    total: int
    created: int
    updated: int
    marked_off_sale: int


async def apply_on_sale_snapshot(
    account_id: str, remote_items: Sequence[RemoteItem], *, synced_at: datetime | None = None
) -> ItemSyncResult:
    """将一份完整在售快照原子写入本地镜像。

    ``remote_items`` 必须来自成功完成的全量分页请求。列表中没有的既有商品会被标记为
    非在售; 远端数据不会被写回或修改。
    """
    now = synced_at or datetime.now(UTC)
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            msg = f"账号 {account_id} 不存在"
            raise ValueError(msg)
        remote_by_id = {item.item_id: item for item in remote_items}
        existing_rows = list(
            (await session.execute(select(Item).where(Item.account_id == account.id)))
            .scalars()
            .all()
        )
        existing_by_id = {item.item_id: item for item in existing_rows}
        created = 0
        updated = 0
        for item_id, remote in remote_by_id.items():
            row = existing_by_id.get(item_id)
            if row is None:
                session.add(
                    Item(
                        account_id=account.id,
                        item_id=remote.item_id,
                        title=remote.title,
                        price=remote.price,
                        status=remote.status,
                        detail_url=remote.detail_url,
                        main_image_url=remote.main_image_url,
                        category_id=remote.category_id,
                        auction_type=remote.auction_type,
                        raw_payload=remote.raw_payload,
                        is_on_sale=True,
                        first_seen_at=now,
                        last_synced_at=now,
                    )
                )
                created += 1
                continue
            row.title = remote.title
            row.price = remote.price
            row.status = remote.status
            row.detail_url = remote.detail_url
            row.main_image_url = remote.main_image_url
            row.category_id = remote.category_id
            row.auction_type = remote.auction_type
            row.raw_payload = remote.raw_payload
            row.is_on_sale = True
            row.last_synced_at = now
            updated += 1
        result = await session.execute(
            update(Item)
            .where(Item.account_id == account.id)
            .where(Item.item_id.not_in(remote_by_id.keys()))
            .where(Item.is_on_sale.is_(True))
            .values(is_on_sale=False, last_synced_at=now)
        )
        await session.commit()
        return ItemSyncResult(
            account_id=account_id,
            total=len(remote_by_id),
            created=created,
            updated=updated,
            marked_off_sale=result.rowcount or 0,
        )


async def list_items(
    account_id: str, *, on_sale_only: bool = True, limit: int = 100
) -> Sequence[Item]:
    """列出本地镜像中的商品。"""
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return []
        stmt = select(Item).where(Item.account_id == account.id)
        if on_sale_only:
            stmt = stmt.where(Item.is_on_sale.is_(True))
        stmt = stmt.order_by(Item.last_synced_at.desc(), Item.id.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())


async def get_item(item_pk: int) -> Item | None:
    """按本地主键读取一个商品镜像。"""
    async with get_async_session() as session:
        return (
            await session.execute(select(Item).where(Item.id == item_pk).limit(1))
        ).scalar_one_or_none()


async def get_item_by_external_id(account_id: str, item_id: str) -> Item | None:
    """Read one account-scoped local item mirror by platform item id."""
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return None
        return (
            await session.execute(
                select(Item).where(Item.account_id == account.id, Item.item_id == item_id).limit(1)
            )
        ).scalar_one_or_none()


async def apply_sold_counts(account_id: str, counts: Mapping[str, int]) -> int:
    """Write aggregated sold counts onto mirrored items; returns rows touched.

    Only items present in counts are updated, so a partial aggregation never
    zeroes out the rest of the catalogue.
    """
    if not counts:
        return 0
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return 0
        rows = list(
            (
                await session.execute(
                    select(Item)
                    .where(Item.account_id == account.id)
                    .where(Item.item_id.in_(list(counts)))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.sold_count = max(int(counts.get(row.item_id, 0)), 0)
        await session.commit()
        return len(rows)


async def set_automation_override(
    account_id: str,
    item_id: str,
    *,
    mode: str | None,
    note: str | None = None,
    actor: str = "operator",
    at: datetime | None = None,
) -> bool:
    """给一件商品打上/清除人工覆盖;mode=None 表示恢复由策略判定。"""
    if mode is not None and mode not in OVERRIDE_MODES:
        msg = f"未知的人工覆盖模式: {mode!r},可选 {OVERRIDE_MODES} 或留空表示清除"
        raise ValueError(msg)
    resolved_note = (note or "").strip() or None
    if mode == OVERRIDE_KEEP and resolved_note is None:
        # 锁定必须能解释清楚:以后看到这件商品被跳过时,原因就在数据里。
        msg = "人工锁定必须写明原因(--note)"
        raise ValueError(msg)
    async with get_async_session() as session:
        row = await _find_item_row(session, account_id=account_id, item_id=item_id)
        if row is None:
            return False
        row.automation_override = mode
        row.automation_note = resolved_note
        row.automation_overridden_at = None if mode is None else (at or datetime.now(UTC))
        row.automation_overridden_by = None if mode is None else actor.strip() or "operator"
        await session.commit()
        return True


async def list_automation_overrides(
    account_id: str, *, limit: int = 200
) -> Sequence[DomainItem]:
    """列出该账号所有带人工覆盖的商品。"""
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            return []
        rows = list(
            (
                await session.execute(
                    select(Item)
                    .where(Item.account_id == account.id)
                    .where(Item.automation_override.is_not(None))
                    .order_by(Item.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [from_row(row) for row in rows]


async def set_item_details(
    account_id: str,
    item_id: str,
    *,
    description: str | None = None,
    images: Sequence[str] | None = None,
    stock: int | None = None,
    has_multi_spec: bool | None = None,
    detail_url: str | None = None,
    synced_at: datetime | None = None,
) -> bool:
    """Write detail fields onto one mirrored item; None means leave unchanged."""
    async with get_async_session() as session:
        row = await _find_item_row(session, account_id=account_id, item_id=item_id)
        if row is None:
            return False
        if description is not None:
            row.description = description
        if images is not None:
            row.images = [str(url) for url in images]
        if stock is not None:
            row.stock = stock
        if has_multi_spec is not None:
            row.has_multi_spec = has_multi_spec
        if detail_url is not None:
            row.detail_url = detail_url
        row.last_synced_at = synced_at or datetime.now(UTC)
        await session.commit()
        return True


async def update_local_item(  # noqa: PLR0912 - field validation and patch branches
    account_id: str,
    item_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    price: str | None = None,
    category_id: str | None = None,
    detail_url: str | None = None,
    stock: int | None = None,
) -> DomainItem | None:
    """Update the local catalog only; never sends an item edit to the platform."""
    if title is not None and not title.strip():
        raise ValueError("商品标题不能为空")
    normalized_price: str | None = None
    if price is not None:
        try:
            amount = Decimal(price.strip())
        except (InvalidOperation, AttributeError) as exc:
            raise ValueError("商品价格格式不合法") from exc
        if amount < 0:
            raise ValueError("商品价格不能为负数")
        normalized_price = format(amount, "f")
    if stock is not None and stock < 0:
        raise ValueError("商品库存不能为负数")
    if not any(value is not None for value in (title, description, price, category_id, detail_url, stock)):
        raise ValueError("至少提供一项商品字段修改")
    async with get_async_session() as session:
        row = await _find_item_row(session, account_id=account_id, item_id=item_id)
        if row is None:
            return None
        if title is not None:
            row.title = title.strip()
        if description is not None:
            row.description = description
        if normalized_price is not None:
            row.price = normalized_price
        if category_id is not None:
            row.category_id = category_id.strip() or None
        if detail_url is not None:
            row.detail_url = detail_url.strip() or None
        if stock is not None:
            row.stock = stock
        row.last_synced_at = datetime.now(UTC)
        await session.commit()
        return from_row(row)


async def archive_local_item(account_id: str, item_id: str) -> bool:
    """Archive a local catalog item while retaining its order history."""
    async with get_async_session() as session:
        row = await _find_item_row(session, account_id=account_id, item_id=item_id)
        if row is None:
            return False
        row.is_on_sale = False
        row.last_synced_at = datetime.now(UTC)
        await session.commit()
        return True


async def list_variants(item_pk: int) -> Sequence[ItemVariant]:
    """Return the persisted spec rows of one mirrored item."""
    async with get_async_session() as session:
        stmt = (
            select(ItemVariant).where(ItemVariant.item_pk == item_pk).order_by(ItemVariant.id.asc())
        )
        return list((await session.execute(stmt)).scalars().all())


async def replace_platform_variants(item_pk: int, variants: Sequence[DomainVariant]) -> int:
    """Converge local specs on the platform snapshot; returns rows kept or added.

    Only rows carrying an external_sku_id are touched: specs the operator declared
    locally have no platform id and must survive a platform sync.
    """
    async with get_async_session() as session:
        existing = list(
            (
                await session.execute(
                    select(ItemVariant)
                    .where(ItemVariant.item_pk == item_pk)
                    .where(ItemVariant.external_sku_id.is_not(None))
                )
            )
            .scalars()
            .all()
        )
        by_sku = {row.external_sku_id: row for row in existing}
        seen: set[str] = set()
        for variant in variants:
            sku_id = (variant.external_sku_id or "").strip()
            if not sku_id:
                continue
            seen.add(sku_id)
            row = by_sku.get(sku_id)
            if row is None:
                row = ItemVariant(item_pk=item_pk, external_sku_id=sku_id)
                session.add(row)
            row.attributes = specs_to_stored(variant.specs)
            row.price = None if variant.price is None else str(variant.price)
            row.stock = variant.stock
            row.image_url = variant.image_url
            row.enabled = variant.enabled
        for sku_id, stale in by_sku.items():
            if sku_id not in seen:
                await session.delete(stale)
        await session.commit()
        return len(seen)


async def _find_item_row(session, *, account_id: str, item_id: str) -> Item | None:
    account = (
        await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
    ).scalar_one_or_none()
    if account is None:
        return None
    return (
        await session.execute(
            select(Item).where(Item.account_id == account.id, Item.item_id == item_id).limit(1)
        )
    ).scalar_one_or_none()
