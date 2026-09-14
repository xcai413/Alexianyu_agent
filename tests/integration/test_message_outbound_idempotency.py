from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Message, database as db_mod, get_async_session
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
