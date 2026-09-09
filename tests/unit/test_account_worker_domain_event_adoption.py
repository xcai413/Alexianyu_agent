"""Contract coverage for AccountWorker canonical Domain Event adoption."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, Message, Order, database as db_mod
from xianyu_agent.db.models import OrderStatus
from xianyu_agent.domain import accounts as domain_accounts, events as domain_events
from xianyu_agent.domain.message import messages as domain_messages
from xianyu_agent.domain.order import orders as domain_orders
from xianyu_agent.protocol import events as protocol_events
from xianyu_agent.runtime import account_worker as account_worker_module
from xianyu_agent.runtime.account_worker import AccountWorker

NOW = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)
RAW_SECRET = {
    "headers": {
        "cookie": "COOKIE-SECRET",
        "authorization": "Bearer TOKEN-SECRET",
    },
    "body": {"content": "BUYER-PRIVATE-TEXT", "buyerId": "buyer-secret-id"},
}


class _FakeClient:
    state = protocol_events.ConnectionState.IDLE

    def __init__(self) -> None:
        self.on_auth_failure = None

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, _text: str) -> bool:
        return True

    def inject_frame(self, _frame: object) -> None:
        return None


class _AllowGuardrails:
    async def check_message(self, _account_id: str, _content: str) -> SimpleNamespace:
        return SimpleNamespace(allowed=True, reason=None)


class _ReplySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[domain_events.MessageReceived, int | None]] = []

    async def handle(
        self,
        event: domain_events.MessageReceived,
        *,
        message_id: int | None = None,
    ) -> None:
        self.calls.append((event, message_id))


class _DeliverySpy:
    def __init__(self) -> None:
        self.calls: list[domain_events.OrderPaid] = []

    async def deliver(self, event: domain_events.OrderPaid) -> SimpleNamespace:
        self.calls.append(event)
        return SimpleNamespace(delivered=False)


def _worker(
    *,
    reply: _ReplySpy | None = None,
    delivery: _DeliverySpy | None = None,
) -> AccountWorker:
    return AccountWorker(
        "acc-1",
        client=_FakeClient(),
        reply_engine=reply,  # type: ignore[arg-type]
        delivery_service=delivery,  # type: ignore[arg-type]
        guardrails=_AllowGuardrails(),  # type: ignore[arg-type]
        automation_mode="passive",
    )


def _assert_raw_is_separate_and_redacted(
    event: domain_events.DomainEvent,
    raw_payload: dict[str, Any] | None,
) -> None:
    assert "raw" not in type(event).model_fields
    assert "raw" not in event.model_dump()
    assert raw_payload is not None
    serialized = json.dumps(raw_payload, ensure_ascii=False)
    assert "COOKIE-SECRET" not in serialized
    assert "TOKEN-SECRET" not in serialized
    assert "BUYER-PRIVATE-TEXT" not in serialized
    assert "buyer-secret-id" not in serialized


@pytest.mark.asyncio
async def test_inbound_maps_once_then_routes_canonical_event_and_separate_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[tuple[domain_events.MessageReceived, dict[str, Any] | None]] = []
    reply = _ReplySpy()

    async def persist(
        event: domain_events.MessageReceived,
        *,
        raw_payload: dict[str, Any] | None = None,
    ) -> int:
        persisted.append((event, raw_payload))
        return 42

    monkeypatch.setattr(account_worker_module.domain_messages, "upsert_inbound", persist)
    event = protocol_events.MessageReceived(
        event_id="evt-in",
        account_id="acc-1",
        received_at=NOW,
        raw=RAW_SECRET,
        chat_id="chat-1",
        message_id="msg-1",
        item_id="item-1",
        sent_at=NOW,
        sender_id="buyer-1",
        sender_name="Buyer",
        content="hello",
    )

    await _worker(reply=reply)._on_event(event)

    assert len(persisted) == 1
    canonical, raw_payload = persisted[0]
    assert type(canonical) is domain_events.MessageReceived
    assert canonical.content == "hello"
    _assert_raw_is_separate_and_redacted(canonical, raw_payload)
    assert reply.calls == [(canonical, 42)]


@pytest.mark.asyncio
async def test_outbound_audit_consumer_receives_canonical_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[domain_events.MessageSent] = []

    async def persist(event: domain_events.MessageSent) -> int:
        captured.append(event)
        return 7

    monkeypatch.setattr(account_worker_module.domain_messages, "record_outbound", persist)
    event = protocol_events.MessageSent(
        event_id="evt-out",
        account_id="acc-1",
        received_at=NOW,
        raw=RAW_SECRET,
        chat_id="chat-1",
        message_id="msg-out",
        receiver_id="buyer-1",
        content="sent text",
    )

    await _worker()._on_event(event)

    assert len(captured) == 1
    assert type(captured[0]) is domain_events.MessageSent
    assert captured[0].content == "sent text"
    assert "raw" not in captured[0].model_dump()


@pytest.mark.asyncio
async def test_paid_order_routes_canonical_event_to_persistence_and_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[tuple[domain_events.OrderPaid, dict[str, Any] | None]] = []
    delivery = _DeliverySpy()

    async def persist(
        event: domain_events.OrderPaid,
        *,
        raw_payload: dict[str, Any] | None = None,
    ) -> int:
        persisted.append((event, raw_payload))
        return 9

    monkeypatch.setattr(account_worker_module.domain_orders, "upsert_from_event", persist)
    event = protocol_events.OrderPaid(
        event_id="evt-order",
        account_id="acc-1",
        received_at=NOW,
        raw=RAW_SECRET,
        order_id="order-1",
        item_id="item-1",
        item_title="Item",
        buyer_id="buyer-1",
        buyer_name="Buyer",
        amount=19.9,
        paid_at=NOW,
    )

    await _worker(delivery=delivery)._on_event(event)

    assert len(persisted) == 1
    canonical, raw_payload = persisted[0]
    assert type(canonical) is domain_events.OrderPaid
    assert canonical.order_id == "order-1"
    _assert_raw_is_separate_and_redacted(canonical, raw_payload)
    assert delivery.calls == [canonical]


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "domain-event-adoption.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_canonical_message_and_order_persistence_accept_separate_raw_metadata(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    raw_metadata = {"transport": "redacted-metadata"}

    inbound = domain_events.MessageReceived(
        event_id="evt-db-in",
        account_id="acc-1",
        received_at=NOW,
        chat_id="chat-db",
        message_id="msg-db-in",
        sender_id="buyer-1",
        content="hello",
    )
    outbound = domain_events.MessageSent(
        event_id="evt-db-out",
        account_id="acc-1",
        received_at=NOW,
        chat_id="chat-db",
        message_id="msg-db-out",
        receiver_id="buyer-1",
        content="reply",
    )
    paid = domain_events.OrderPaid(
        event_id="evt-db-order",
        account_id="acc-1",
        received_at=NOW,
        order_id="order-db",
        item_id="item-db",
        buyer_id="buyer-1",
        amount=8.8,
        paid_at=NOW,
    )

    await domain_messages.upsert_inbound(inbound, raw_payload=raw_metadata)
    await domain_messages.record_outbound(outbound)
    await domain_orders.upsert_from_event(paid, raw_payload=raw_metadata)

    async with db_mod.get_async_session() as session:
        messages = list(
            (
                await session.execute(
                    select(Message).where(Message.chat_id == "chat-db").order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
        order = (
            await session.execute(select(Order).where(Order.order_id == "order-db").limit(1))
        ).scalar_one()

    assert [row.direction for row in messages] == ["inbound", "outbound"]
    assert messages[0].raw_payload == raw_metadata
    assert messages[1].raw_payload is None
    assert order.status == OrderStatus.PAID.value
    assert order.raw_payload == raw_metadata
    assert "raw" not in inbound.model_dump()
    assert "raw" not in paid.model_dump()


@pytest.mark.asyncio
async def test_system_notice_keeps_audit_behavior_without_transport_raw(clean_db) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    event = protocol_events.SystemNotice(
        event_id="evt-notice",
        account_id="acc-1",
        received_at=NOW,
        raw=RAW_SECRET,
        notice_type="system_tip",
        content="safe notice",
    )

    await _worker()._on_event(event)

    async with db_mod.get_async_session() as session:
        row = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "protocol.system_notice").limit(1)
            )
        ).scalar_one()

    assert row.target == "acc-1"
    assert row.params == {"notice_type": "system_tip", "content": "safe notice"}
    serialized = json.dumps(row.params, ensure_ascii=False)
    assert "COOKIE-SECRET" not in serialized
    assert "TOKEN-SECRET" not in serialized
