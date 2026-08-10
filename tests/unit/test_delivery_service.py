"""Unit tests for DeliveryService: success / no-card / send-failure paths."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import CardConsumption, Order, database as db_mod
from xianyu_agent.db.models import OrderStatus
from xianyu_agent.domain import (
    accounts as domain_accounts,
    cards as domain_cards,
    orders as domain_orders,
)
from xianyu_agent.protocol.events import OrderDelivered, OrderPaid
from xianyu_agent.services.delivery_service import DeliveryService


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "delivery.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _paid_event(account_id: str = "acc-1", order_id: str = "O-1") -> OrderPaid:
    return OrderPaid(
        event_id="e-1",
        account_id=account_id,
        received_at=datetime.now(UTC),
        order_id=order_id,
        item_id="I-1",
        item_title="好物",
        buyer_id="buyer-1",
        buyer_name="买家",
        amount=9.9,
        paid_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_deliver_success(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    card = await domain_cards.create_card("acc-1", "卡", "CODE-9", type_="text")
    sent: list[tuple[str, str, str]] = []

    async def sender(account_id: str, order_id: str, content: str) -> bool:
        sent.append((account_id, order_id, content))
        return True

    svc = DeliveryService(sender=sender)
    result = await svc.deliver(_paid_event())

    assert result.delivered is True
    assert result.code == "CODE-9"
    assert sent == [("acc-1", "O-1", "CODE-9")]

    async with db_mod.get_async_session() as session:
        order = (
            await session.execute(select(Order).where(Order.order_id == "O-1").limit(1))
        ).scalar_one()
        cons = (
            (
                await session.execute(
                    select(CardConsumption).where(CardConsumption.card_id == card.id)
                )
            )
            .scalars()
            .all()
        )
    assert order.status == OrderStatus.DELIVERED.value
    assert order.delivery_content == "CODE-9"
    assert order.delivered_at is not None
    assert order.delivery_fail_reason is None
    assert len(cons) == 1
    assert cons[0].status == "success"
    assert cons[0].content == "CODE-9"

    # card stock decremented
    got = await domain_cards.get_card(card.id)
    assert got.remaining == 0


@pytest.mark.asyncio
async def test_deliver_no_card(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    svc = DeliveryService(sender=lambda a, o, c: True)
    result = await svc.deliver(_paid_event())
    assert result.delivered is False
    assert "无可用卡密" in (result.reason or "")

    async with db_mod.get_async_session() as session:
        order = (
            await session.execute(select(Order).where(Order.order_id == "O-1").limit(1))
        ).scalar_one()
    assert order.delivery_fail_reason == "无可用卡密"


@pytest.mark.asyncio
async def test_deliver_send_failure_recorded(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    card = await domain_cards.create_card("acc-1", "卡", "CODE-X", type_="text")

    async def sender(account_id: str, order_id: str, content: str) -> bool:
        return False

    svc = DeliveryService(sender=sender)
    result = await svc.deliver(_paid_event())
    assert result.delivered is False

    async with db_mod.get_async_session() as session:
        order = (
            await session.execute(select(Order).where(Order.order_id == "O-1").limit(1))
        ).scalar_one()
        cons = (
            (
                await session.execute(
                    select(CardConsumption).where(CardConsumption.card_id == card.id)
                )
            )
            .scalars()
            .all()
        )
    assert order.delivery_fail_reason is not None
    assert cons[0].status == "failed"
    # card stock still decremented (code is reserved for retry)
    got = await domain_cards.get_card(card.id)
    assert got.remaining == 0


@pytest.mark.asyncio
async def test_upstream_delivered_does_not_mask_send_failure(clean_db) -> None:
    """An OrderDelivered event must not flip status if we never delivered."""
    await domain_accounts.create_account("acc-1", enabled=True)
    card = await domain_cards.create_card("acc-1", "卡", "CODE-Z", type_="text")

    async def sender(account_id: str, order_id: str, content: str) -> bool:
        return False  # send always fails

    svc = DeliveryService(sender=sender)
    result = await svc.deliver(_paid_event())
    assert result.delivered is False

    # A later upstream delivered-ack arrives without our content.
    ack = OrderDelivered(
        event_id="e-ack",
        account_id="acc-1",
        received_at=datetime.now(UTC),
        order_id="O-1",
        delivered_at=datetime.now(UTC),
    )
    await domain_orders.upsert_from_event(ack)

    async with db_mod.get_async_session() as session:
        order = (
            await session.execute(select(Order).where(Order.order_id == "O-1").limit(1))
        ).scalar_one()
    assert order.status == OrderStatus.PAID.value  # not delivered
    assert order.delivery_fail_reason is not None

    # Sanity: card still exists (not consumed twice)
    got = await domain_cards.get_card(card.id)
    assert got.remaining == 0


@pytest.mark.asyncio
async def test_retry_send_failure_reuses_reserved_code(clean_db) -> None:
    """retry() re-sends the reserved code and flips the order to delivered."""
    await domain_accounts.create_account("acc-1", enabled=True)
    card = await domain_cards.create_card("acc-1", "卡", "CODE-R", type_="text")

    async def sender(account_id: str, order_id: str, content: str) -> bool:
        return False

    svc = DeliveryService(sender=sender)
    first = await svc.deliver(_paid_event(order_id="O-R"))
    assert first.delivered is False

    async with db_mod.get_async_session() as session:
        order = (
            await session.execute(select(Order).where(Order.order_id == "O-R").limit(1))
        ).scalar_one()
        order_id = order.id
        cons = (
            (
                await session.execute(
                    select(CardConsumption).where(CardConsumption.card_id == card.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(cons) == 1
    assert cons[0].status == "failed"
    assert cons[0].content == "CODE-R"

    # Now the sender recovers; retry must reuse CODE-R (not consume a 2nd code).
    sent: list[tuple[str, str, str]] = []

    async def ok_sender(account_id: str, order_id: str, content: str) -> bool:
        sent.append((account_id, order_id, content))
        return True

    svc2 = DeliveryService(sender=ok_sender)
    result = await svc2.retry(order_id)
    assert result.delivered is True
    assert result.code == "CODE-R"
    assert sent == [("acc-1", "O-R", "CODE-R")]

    async with db_mod.get_async_session() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id).limit(1))).scalar_one()
        cons = (
            (
                await session.execute(
                    select(CardConsumption).where(CardConsumption.card_id == card.id)
                )
            )
            .scalars()
            .all()
        )
    assert order.status == OrderStatus.DELIVERED.value
    assert order.delivery_content == "CODE-R"
    assert order.delivery_fail_reason is None
    assert len(cons) == 1
    assert cons[0].status == "success"
    # stock not double-decremented
    got = await domain_cards.get_card(card.id)
    assert got.remaining == 0


@pytest.mark.asyncio
async def test_retry_missing_order(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    svc = DeliveryService(sender=lambda a, o, c: True)
    result = await svc.retry(99999)
    assert result.delivered is False
    assert "订单不存在" in (result.reason or "")


@pytest.mark.asyncio
async def test_retry_no_reserved_code(clean_db) -> None:
    """Guardrail-blocked order (no consumption) cannot be retried blindly."""
    await domain_accounts.create_account("acc-1", enabled=True)
    svc = DeliveryService(sender=lambda a, o, c: True)
    ev = _paid_event(order_id="O-H")
    ev.amount = 99999.0
    await svc.deliver(ev)

    async with db_mod.get_async_session() as session:
        order = (
            await session.execute(select(Order).where(Order.order_id == "O-H").limit(1))
        ).scalar_one()
        order_id = order.id
    result = await svc.retry(order_id)
    assert result.delivered is False
    assert "无预留卡密" in (result.reason or "")
