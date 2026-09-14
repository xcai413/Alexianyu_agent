from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from xianyu_agent.application.message import SendAttemptStatus, SendMessageService


class FakeRecorder:
    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    async def record_attempt(self, **kwargs: Any) -> None:
        self.attempts.append(kwargs)

    async def record_result(self, **kwargs: Any) -> None:
        self.results.append(kwargs)


class FakeProtocol:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def send_text_message(self, *, chat_id: str, receiver_id: str, text: str) -> Any:
        self.calls.append({"chat_id": chat_id, "receiver_id": receiver_id, "text": text})
        return SimpleNamespace(
            request_id="mid-1",
            client_message_id="client-1",
            response={"code": 200, "body": {"messageId": "platform-1"}},
        )


@dataclass
class FakeStore:
    receiver: str | None = "buyer-1"
    resolve_error: Exception | None = None
    persist_error: Exception | None = None

    def __post_init__(self) -> None:
        self.persisted: list[dict[str, Any]] = []

    async def resolve_receiver(self, *, account_id: str, chat_id: str) -> str | None:
        if self.resolve_error is not None:
            raise self.resolve_error
        assert account_id == "acc-1"
        assert chat_id == "chat-1"
        return self.receiver

    async def record_outbound(self, **kwargs: Any) -> None:
        if self.persist_error is not None:
            raise self.persist_error
        self.persisted.append(kwargs)


@pytest.mark.asyncio
async def test_service_resolves_receiver_from_canonical_store_before_write() -> None:
    protocol = FakeProtocol()
    recorder = FakeRecorder()
    store = FakeStore(receiver="buyer-42")
    service = SendMessageService(protocol, recorder, store)

    result = await service.send_text(account_id="acc-1", chat_id="chat-1", text="hello")

    assert result.status is SendAttemptStatus.SUCCESS
    assert result.receiver_id == "buyer-42"
    assert protocol.calls == [
        {"chat_id": "chat-1", "receiver_id": "buyer-42", "text": "hello"}
    ]
    assert len(recorder.attempts) == 1
    assert len(store.persisted) == 1
    assert len(recorder.results) == 1


@pytest.mark.asyncio
async def test_receiver_lookup_failure_is_retryable_and_performs_no_external_write() -> None:
    protocol = FakeProtocol()
    recorder = FakeRecorder()
    store = FakeStore(resolve_error=RuntimeError("db unavailable"))
    service = SendMessageService(protocol, recorder, store)

    result = await service.send_text(account_id="acc-1", chat_id="chat-1", text="hello")

    assert result.status is SendAttemptStatus.FAILED_RETRYABLE
    assert result.retry_allowed is True
    assert protocol.calls == []
    assert recorder.attempts == []
    assert recorder.results == []


@pytest.mark.asyncio
async def test_missing_conversation_receiver_fails_final_without_write() -> None:
    protocol = FakeProtocol()
    recorder = FakeRecorder()
    service = SendMessageService(protocol, recorder, FakeStore(receiver=None))

    result = await service.send_text(account_id="acc-1", chat_id="chat-1", text="hello")

    assert result.status is SendAttemptStatus.FAILED_FINAL
    assert result.retry_allowed is False
    assert protocol.calls == []


@pytest.mark.asyncio
async def test_platform_success_plus_outbound_persistence_failure_requires_reconciliation() -> None:
    protocol = FakeProtocol()
    recorder = FakeRecorder()
    store = FakeStore(persist_error=RuntimeError("commit failed"))
    service = SendMessageService(protocol, recorder, store)

    result = await service.send_text(account_id="acc-1", chat_id="chat-1", text="hello")

    assert len(protocol.calls) == 1
    assert result.status is SendAttemptStatus.RECONCILIATION_REQUIRED
    assert result.retry_allowed is False
    assert "outbound persistence failed after platform success" in (result.detail or "")
    assert recorder.results[-1]["status"] is SendAttemptStatus.RECONCILIATION_REQUIRED
