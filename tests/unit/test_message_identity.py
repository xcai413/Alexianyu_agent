"""多账号消息幂等与观察模式测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Conversation, Message, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, messages as domain_messages
from xianyu_agent.protocol.events import MessageReceived


@pytest.fixture
async def message_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "messages.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-a", enabled=True)
    await domain_accounts.create_account("acc-b", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _event(account_id: str, content: str = "hello") -> MessageReceived:
    return MessageReceived(
        event_id=f"event-{account_id}",
        account_id=account_id,
        received_at=datetime.now(UTC),
        chat_id="chat-1",
        message_id="same-upstream-id",
        item_id="item-1",
        sender_id="buyer-1",
        content=content,
    )


@pytest.mark.asyncio
async def test_duplicate_replay_is_idempotent_per_account(message_db) -> None:
    first = await domain_messages.upsert_inbound(_event("acc-a"))
    duplicate = await domain_messages.upsert_inbound(_event("acc-a", "changed"))
    assert first == duplicate
    async with get_async_session() as session:
        count = await session.scalar(select(func.count()).select_from(Message))
        row = (await session.execute(select(Message))).scalar_one()
    assert count == 1
    assert row.content == "hello"
    assert row.item_id == "item-1"
    assert row.external_message_id == "same-upstream-id"
    async with get_async_session() as session:
        conversation = (await session.execute(select(Conversation))).scalar_one()
    assert row.conversation_id == conversation.id
    assert conversation.buyer_id == "buyer-1"
    assert conversation.external_conversation_id == "chat-1"
    assert conversation.unread_count == 1


@pytest.mark.asyncio
async def test_same_upstream_id_is_valid_for_two_accounts(message_db) -> None:
    first = await domain_messages.upsert_inbound(_event("acc-a"))
    second = await domain_messages.upsert_inbound(_event("acc-b"))
    assert first is not None
    assert second is not None
    assert first != second
    async with get_async_session() as session:
        count = await session.scalar(select(func.count()).select_from(Message))
        conversations = list((await session.execute(select(Conversation))).scalars())
    assert count == 2
    assert len(conversations) == 2
    assert {row.account_id for row in conversations} == {1, 2}
