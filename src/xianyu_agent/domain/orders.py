"""Order persistence helpers."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select

from xianyu_agent.db import Account, Order, get_async_session
from xianyu_agent.db.models import OrderStatus
from xianyu_agent.protocol.events import (
    OrderCreated,
    OrderDelivered,
    OrderPaid,
)

logger = logging.getLogger(__name__)


async def upsert_from_event(
    event: OrderCreated | OrderPaid | OrderDelivered,
) -> int | None:
    """Insert or update an order row from an event.
    Idempotent on (account_id, order_id).
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
                raw_payload=event.raw,
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


async def list_for_account(
    account_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
) -> Sequence[Order]:
    async with get_async_session() as session:
        account = (
            await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
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
