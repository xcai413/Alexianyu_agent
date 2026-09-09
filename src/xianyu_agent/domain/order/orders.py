"""Order persistence helpers."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select

from xianyu_agent.db import Account, Order, get_async_session
from xianyu_agent.db.models import OrderStatus
from xianyu_agent.domain.events import OrderCreated, OrderDelivered, OrderPaid
from xianyu_agent.protocol.orders_client import RemoteSoldOrder

logger = logging.getLogger(__name__)

SELLABLE_ORDER_STATUSES = frozenset({"paid", "delivered", "completed"})


@dataclass(frozen=True)
class OrderSyncResult:
    """一次卖家订单只读快照的本地落库结果。"""

    account_id: str
    total: int
    created: int
    updated: int


@dataclass(frozen=True)
class ItemSalesSummary:
    """按商品汇总的可计入已售件数。"""

    item_id: str
    sold_quantity: int
    sold_order_count: int


async def upsert_from_event(
    event: OrderCreated | OrderPaid | OrderDelivered,
    *,
    raw_payload: dict[str, Any] | None = None,
) -> int | None:
    """Insert or update an order row from a canonical Domain Event.

    Idempotent on ``(account_id, order_id)``. ``raw_payload`` is separately-redacted
    persistence metadata and is deliberately not part of the Domain Event contract.
    """
    async with get_async_session() as session:
        account = (
            await session.execute(
                select(Account).where(Account.account_id == event.account_id).limit(1)
            )
        ).scalar_one_or_none()
        if account is None:
            return None
        stmt = (
            select(Order)
            .where(Order.account_id == account.id, Order.order_id == event.order_id)
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = Order(
                account_id=account.id,
                order_id=event.order_id,
                item_id=getattr(event, "item_id", None),
                item_title=getattr(event, "item_title", None),
                buyer_id=getattr(event, "buyer_id", None) or "unknown",
                buyer_name=getattr(event, "buyer_name", None),
                amount=float(getattr(event, "amount", 0.0) or 0.0),
                quantity=1,
                raw_payload=raw_payload,
            )
            session.add(row)
        else:
            if getattr(event, "item_title", None):
                row.item_title = event.item_title
            if getattr(event, "amount", None) is not None:
                row.amount = float(getattr(event, "amount", 0.0) or 0.0)
            if getattr(event, "buyer_name", None):
                row.buyer_name = event.buyer_name
        if isinstance(event, OrderCreated):
            row.status = OrderStatus.PENDING_PAYMENT.value
        elif isinstance(event, OrderPaid):
            row.status = OrderStatus.PAID.value
            row.paid_at = event.paid_at
        elif isinstance(event, OrderDelivered) and row.delivery_content is not None:
            # Only accept an upstream delivered-confirmation when we actually
            # delivered content; otherwise it could mask a send failure.
            row.status = OrderStatus.DELIVERED.value
            row.delivered_at = event.delivered_at or row.delivered_at
        await session.commit()
        await session.refresh(row)
        return row.id


async def apply_sold_orders_snapshot(
    account_id: str,
    remote_orders: Sequence[RemoteSoldOrder],
) -> OrderSyncResult:
    """将卖家订单只读快照写入本地镜像。

    订单是历史记录: 本次快照缺失的本地订单绝不删除、也不改变其状态。同步不触发
    DeliveryService, 已存在订单的发货内容与失败信息也不会被覆盖。
    """
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            msg = f"账号 {account_id} 不存在"
            raise ValueError(msg)
        remote_by_id = {order.order_id: order for order in remote_orders}
        if not remote_by_id:
            return OrderSyncResult(account_id=account_id, total=0, created=0, updated=0)
        existing_rows = list(
            (
                await session.execute(
                    select(Order).where(
                        Order.account_id == account.id,
                        Order.order_id.in_(remote_by_id),
                    )
                )
            ).scalars()
        )
        existing_by_id = {row.order_id: row for row in existing_rows}
        created = 0
        updated = 0
        for order_id, remote in remote_by_id.items():
            row = existing_by_id.get(order_id)
            if row is None:
                session.add(
                    Order(
                        account_id=account.id,
                        order_id=remote.order_id,
                        item_id=remote.item_id,
                        buyer_id=remote.buyer_id,
                        buyer_name=remote.buyer_name,
                        amount=remote.amount,
                        quantity=remote.quantity,
                        status=remote.status,
                        placed_at=remote.placed_at,
                        raw_payload=remote.raw_payload,
                    )
                )
                created += 1
                continue
            row.item_id = remote.item_id or row.item_id
            row.buyer_id = remote.buyer_id or row.buyer_id
            row.buyer_name = remote.buyer_name or row.buyer_name
            row.amount = remote.amount
            row.quantity = remote.quantity
            row.status = remote.status
            row.placed_at = remote.placed_at or row.placed_at
            row.raw_payload = remote.raw_payload
            updated += 1
        await session.commit()
        return OrderSyncResult(
            account_id=account_id,
            total=len(remote_by_id),
            created=created,
            updated=updated,
        )


async def list_for_account(
    account_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
) -> Sequence[Order]:
    async with get_async_session() as session:
        account = (
            await session.execute(
                select(Account).where(Account.account_id == account_id).limit(1)
            )
        ).scalar_one_or_none()
        if account is None:
            return []
        stmt = select(Order).where(Order.account_id == account.id)
        if status:
            stmt = stmt.where(Order.status == status)
        stmt = stmt.order_by(Order.created_at.desc()).limit(limit)
        return list((await session.execute(stmt)).scalars().all())


async def get_by_id(order_id: int) -> Order | None:
    async with get_async_session() as session:
        return (
            await session.execute(select(Order).where(Order.id == order_id).limit(1))
        ).scalar_one_or_none()


async def sales_by_item(account_id: str) -> dict[str, ItemSalesSummary]:
    """汇总已付款、已发货或交易成功订单的已售件数。

    已关闭、退款中/成功和未知状态不计入, 以免把可逆交易夸大为已售。
    """
    async with get_async_session() as session:
        account = (
            await session.execute(
                select(Account).where(Account.account_id == account_id).limit(1)
            )
        ).scalar_one_or_none()
        if account is None:
            return {}
        stmt = (
            select(
                Order.item_id,
                func.coalesce(func.sum(Order.quantity), 0).label("sold_quantity"),
                func.count(Order.id).label("sold_order_count"),
            )
            .where(
                Order.account_id == account.id,
                Order.item_id.is_not(None),
                Order.status.in_(SELLABLE_ORDER_STATUSES),
            )
            .group_by(Order.item_id)
        )
        rows = (await session.execute(stmt)).all()
        return {
            str(row.item_id): ItemSalesSummary(
                item_id=str(row.item_id),
                sold_quantity=int(row.sold_quantity or 0),
                sold_order_count=int(row.sold_order_count or 0),
            )
            for row in rows
            if row.item_id
        }
