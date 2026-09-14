from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from xianyu_agent.domain.events import MessageContentType, MessageSent
from xianyu_agent.domain.message import messages as domain_messages
from xianyu_agent.infrastructure.database.models import Message
from xianyu_agent.infrastructure.message.message_store import DomainMessageStore
from xianyu_agent.protocol.ws import message_send
from xianyu_agent.protocol.ws.request_router import RequestRouter


@pytest.mark.asyncio
async def test_closed_router_before_registration_is_proven_not_sent() -> None:
    router = RequestRouter()
    router.close()
    writes = 0

    async def send_request(_frame: dict[str, Any]) -> bool:
        nonlocal writes
        writes += 1
        return True

    with pytest.raises(message_send.MessageSendNotSent) as caught:
        await message_send.request_text_message(
            router,
            send_request,
            account_user_id="seller-1",
            chat_id="chat-1",
            receiver_id="buyer-1",
            text="hello",
            mid_factory=lambda: "mid-closed",
            uuid_factory=lambda: "uuid-closed",
        )

    assert writes == 0
    assert caught.value.request_id == "mid-closed"
    assert caught.value.client_message_id == "uuid-closed"


@pytest.mark.asyncio
async def test_receiver_resolution_rejects_unknown_sender_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list_recent(**_kwargs: Any) -> list[SimpleNamespace]:
        return [SimpleNamespace(sender_id="unknown")]

    monkeypatch.setattr(domain_messages, "list_recent", fake_list_recent)

    receiver = await DomainMessageStore().resolve_receiver(
        account_id="acc-1",
        chat_id="chat-1",
    )

    assert receiver is None


@pytest.mark.asyncio
async def test_outbound_unique_conflict_requeries_existing_platform_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.rollback_called = False
            self.added: list[Message] = []

        def add(self, row: Message) -> None:
            self.added.append(row)

        async def commit(self) -> None:
            raise IntegrityError("insert message", {}, RuntimeError("duplicate"))

        async def rollback(self) -> None:
            self.rollback_called = True

        async def refresh(self, _row: Message) -> None:
            raise AssertionError("conflict path must return the existing row")

    session = FakeSession()

    @asynccontextmanager
    async def fake_get_async_session():
        yield session

    async def fake_get_account(_session: Any, _account_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=7)

    existing = Message(
        id=99,
        account_id=7,
        chat_id="chat-1",
        message_id="platform-1",
        sender_id="acc-1",
        direction="outbound",
        content_type="text",
        content="hello",
        received_at=datetime.now(UTC),
    )
    lookups = 0

    async def fake_find_message_by_platform_id(
        _session: Any,
        *,
        account_id: int,
        message_id: str,
    ) -> Message | None:
        nonlocal lookups
        lookups += 1
        assert account_id == 7
        assert message_id == "platform-1"
        return None if lookups == 1 else existing

    monkeypatch.setattr(domain_messages, "get_async_session", fake_get_async_session)
    monkeypatch.setattr(domain_messages, "_get_account", fake_get_account)
    monkeypatch.setattr(
        domain_messages,
        "_find_message_by_platform_id",
        fake_find_message_by_platform_id,
    )

    event = MessageSent(
        event_id="event-1",
        account_id="acc-1",
        received_at=datetime.now(UTC),
        chat_id="chat-1",
        message_id="platform-1",
        receiver_id="buyer-1",
        content_type=MessageContentType.TEXT,
        content="hello",
    )

    row_id = await domain_messages.record_outbound(event)

    assert row_id == 99
    assert session.rollback_called is True
    assert lookups == 2
    assert len(session.added) == 1
