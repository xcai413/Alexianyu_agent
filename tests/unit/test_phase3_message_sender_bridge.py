from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from xianyu_agent.application.message.send_service import SendAttemptStatus
from xianyu_agent.runtime import message_sender


class _Client:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> bool:
        self.sent.append(text)
        return True


@pytest.mark.asyncio
async def test_worker_text_uses_canonical_service_for_ordinary_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    captured: dict[str, Any] = {}

    class Store:
        async def resolve_receiver(self, *, account_id: str, chat_id: str) -> str | None:
            assert (account_id, chat_id) == ("acc-1", "chat-1")
            return "buyer-1"

    class Service:
        def __init__(self, protocol: Any, recorder: Any, store: Any) -> None:
            captured["protocol"] = protocol
            captured["recorder"] = recorder
            captured["store"] = store

        async def send_text(
            self,
            *,
            account_id: str,
            chat_id: str,
            receiver_id: str | None,
            text: str,
        ) -> Any:
            captured.update(
                account_id=account_id,
                chat_id=chat_id,
                receiver_id=receiver_id,
                text=text,
            )
            return SimpleNamespace(status=SendAttemptStatus.SUCCESS)

    async def unexpected_legacy_check(_account_id: str, _chat_id: str) -> bool:
        raise AssertionError("legacy-order fallback must not be checked when receiver is known")

    monkeypatch.setattr(message_sender, "DomainMessageStore", Store)
    monkeypatch.setattr(message_sender, "SendMessageService", Service)
    monkeypatch.setattr(message_sender, "_is_legacy_delivery_order", unexpected_legacy_check)

    ok = await message_sender.send_worker_text(
        client,
        account_id="acc-1",
        chat_id="chat-1",
        text="hello",
    )

    assert ok is True
    assert client.sent == []
    assert captured["account_id"] == "acc-1"
    assert captured["chat_id"] == "chat-1"
    assert captured["receiver_id"] == "buyer-1"
    assert captured["text"] == "hello"
    assert isinstance(captured["store"], Store)


@pytest.mark.asyncio
async def test_worker_text_preserves_legacy_delivery_order_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()

    class Store:
        async def resolve_receiver(self, *, account_id: str, chat_id: str) -> None:
            assert (account_id, chat_id) == ("acc-1", "order-1")

    class UnexpectedService:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("legacy delivery fallback must not construct SendMessageService")

    async def is_legacy_order(account_id: str, order_id: str) -> bool:
        assert (account_id, order_id) == ("acc-1", "order-1")
        return True

    monkeypatch.setattr(message_sender, "DomainMessageStore", Store)
    monkeypatch.setattr(message_sender, "SendMessageService", UnexpectedService)
    monkeypatch.setattr(message_sender, "_is_legacy_delivery_order", is_legacy_order)

    ok = await message_sender.send_worker_text(
        client,
        account_id="acc-1",
        chat_id="order-1",
        text="delivery-code",
    )

    assert ok is True
    assert client.sent == ["delivery-code"]


@pytest.mark.asyncio
async def test_worker_text_does_not_fallback_for_unknown_non_order_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    captured: dict[str, Any] = {}

    class Store:
        async def resolve_receiver(self, *, account_id: str, chat_id: str) -> None:
            assert (account_id, chat_id) == ("acc-1", "chat-unknown")

    class Service:
        def __init__(self, _protocol: Any, _recorder: Any, _store: Any) -> None:
            pass

        async def send_text(
            self,
            *,
            account_id: str,
            chat_id: str,
            receiver_id: str | None,
            text: str,
        ) -> Any:
            captured.update(
                account_id=account_id,
                chat_id=chat_id,
                receiver_id=receiver_id,
                text=text,
            )
            return SimpleNamespace(status=SendAttemptStatus.FAILED_RETRYABLE)

    async def is_legacy_order(account_id: str, order_id: str) -> bool:
        assert (account_id, order_id) == ("acc-1", "chat-unknown")
        return False

    monkeypatch.setattr(message_sender, "DomainMessageStore", Store)
    monkeypatch.setattr(message_sender, "SendMessageService", Service)
    monkeypatch.setattr(message_sender, "_is_legacy_delivery_order", is_legacy_order)

    ok = await message_sender.send_worker_text(
        client,
        account_id="acc-1",
        chat_id="chat-unknown",
        text="hello",
    )

    assert ok is False
    assert client.sent == []
    assert captured == {
        "account_id": "acc-1",
        "chat_id": "chat-unknown",
        "receiver_id": None,
        "text": "hello",
    }
