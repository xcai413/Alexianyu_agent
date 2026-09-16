from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Conversation, Message, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.events import MessageContentType, MessageSent
from xianyu_agent.domain.message import messages as domain_messages


@pytest.mark.asyncio
async def test_record_outbound_merges_same_platform_message_id(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "outbound-idempotency.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    await domain_accounts.create_account("acc-idem", enabled=True)
    first = MessageSent(
        event_id="event-ack",
        account_id="acc-idem",
        received_at=datetime.now(UTC),
        chat_id="chat-idem",
        message_id="platform-message-1",
        receiver_id="buyer-idem",
        content_type=MessageContentType.TEXT,
        content="hello",
    )
    pushed = MessageSent(
        event_id="event-push",
        account_id="acc-idem",
        received_at=datetime.now(UTC),
        chat_id="chat-idem",
        message_id="platform-message-1",
        receiver_id="buyer-idem",
        content_type=MessageContentType.TEXT,
        content="hello",
    )

    first_id = await domain_messages.record_outbound(first)
    pushed_id = await domain_messages.record_outbound(pushed)

    async with get_async_session() as session:
        rows = list((await session.execute(select(Message))).scalars().all())
    assert first_id is not None
    assert pushed_id == first_id
    assert len(rows) == 1
    assert rows[0].message_id == "platform-message-1"
    assert rows[0].content == "hello"

    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_duplicate_outbound_event_does_not_advance_conversation_time(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "outbound-replay-time.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-replay", enabled=True)

    original_time = datetime(2026, 1, 1, tzinfo=UTC)
    replay_time = datetime(2026, 1, 2, tzinfo=UTC)
    acknowledged = MessageSent(
        event_id="event-ack",
        account_id="acc-replay",
        received_at=original_time,
        chat_id="chat-replay",
        message_id="platform-message-replay",
        receiver_id="buyer-replay",
        content_type=MessageContentType.TEXT,
        content="hello",
    )
    replayed = acknowledged.model_copy(update={"received_at": replay_time})

    await domain_messages.record_outbound(acknowledged)
    await domain_messages.record_outbound(replayed)

    async with get_async_session() as session:
        conversation = (await session.execute(select(Conversation))).scalar_one()
    assert conversation.last_message_at == original_time.replace(tzinfo=None)

    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_outbound_conversation_backfills_unknown_buyer_and_item(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "outbound-conversation.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-conversation", enabled=True)

    acknowledged = MessageSent(
        event_id="event-ack",
        account_id="acc-conversation",
        received_at=datetime.now(UTC),
        chat_id="chat-conversation",
        message_id="platform-message-2",
        receiver_id="unknown",
        content_type=MessageContentType.TEXT,
        content="hello",
    )
    seller_push = acknowledged.model_copy(
        update={"receiver_id": "buyer-conversation", "item_id": "item-conversation"}
    )

    await domain_messages.record_outbound(acknowledged)
    await domain_messages.record_outbound(seller_push)

    async with get_async_session() as session:
        conversation = (await session.execute(select(Conversation))).scalar_one()
    assert conversation.buyer_id == "buyer-conversation"
    assert conversation.item_id == "item-conversation"

    await db_mod.async_engine.dispose()
    reset_settings_cache()
