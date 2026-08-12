"""账号在售商品本地镜像的领域逻辑。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update

from xianyu_agent.db import Account, Item, get_async_session
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
            (
                await session.execute(select(Item).where(Item.account_id == account.id))
            ).scalars().all()
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
