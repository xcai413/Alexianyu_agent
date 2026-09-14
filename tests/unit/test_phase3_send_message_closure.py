"""Contracts for the Phase 3 outbound text-message closure."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
from typing import Any, ClassVar

import pytest

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws.request_router import RequestRouter


def _protocol_module():
    return importlib.import_module("xianyu_agent.protocol.ws.message_send")


def _application_module():
    return importlib.import_module("xianyu_agent.application.message.send_service")


def test_text_send_frame_matches_calibrated_wire_contract() -> None:
    message_send = _protocol_module()

    frame = message_send.build_text_message_request(
        account_user_id="seller-1",
        chat_id="chat-9",
        receiver_id="buyer-2",
        text="hello",
        mid_factory=lambda: "mid-1",
        uuid_factory=lambda: "uuid-1",
    )

    assert frame["lwp"] == "/r/MessageSend/sendByReceiverScope"
    assert frame["headers"] == {"mid": "mid-1"}
    assert frame["body"][0] == {
        "uuid": "uuid-1",
        "cid": "chat-9@goofish",
        "conversationType": 1,
        "content": {
            "contentType": 101,
            "custom": {
                "type": 1,
                "data": frame["body"][0]["content"]["custom"]["data"],
            },
        },
        "redPointPolicy": 0,
        "extension": {"extJson": "{}"},
        "ctx": {"appVersion": "1.0", "platform": "web"},
        "mtags": {},
        "msgReadStatusSetting": 1,
    }
    assert frame["body"][1] == {
        "actualReceivers": ["buyer-2@goofish", "seller-1@goofish"]
    }

    encoded = frame["body"][0]["content"]["custom"]["data"]
    decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert decoded == {"contentType": 1, "text": {"text": "hello"}}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_user_id", " "),
        ("chat_id", " "),
        ("receiver_id", " "),
        ("text", ""),
    ],
)
def test_text_send_frame_rejects_missing_routing_or_content(field: str, value: str) -> None:
    message_send = _protocol_module()
    kwargs = {
        "account_user_id": "seller-1",
        "chat_id": "chat-9",
        "receiver_id": "buyer-2",
        "text": "hello",
        "mid_factory": lambda: "mid-1",
        "uuid_factory": lambda: "uuid-1",
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        message_send.build_text_message_request(**kwargs)


@pytest.mark.asyncio
async def test_text_send_registers_before_write_and_correlates_ack() -> None:
    message_send = _protocol_module()
    router = RequestRouter()

    async def send_request(frame: dict[str, Any]) -> bool:
        assert router.pending_count == 1
        response = {"code": 200, "body": {"messageId": "platform-msg-1"}}
        matched = router.match_frame(
            WsFrame(headers={"mid": frame["headers"]["mid"]}),
            response,
        )
        assert matched is True
        return True

    receipt = await message_send.request_text_message(
        router,
        send_request,
        account_user_id="seller-1",
        chat_id="chat-9",
        receiver_id="buyer-2",
        text="hello",
        timeout_s=0.1,
        mid_factory=lambda: "mid-1",
        uuid_factory=lambda: "uuid-1",
    )

    assert receipt.response == {"code": 200, "body": {"messageId": "platform-msg-1"}}
    assert receipt.request_id == "mid-1"
    assert receipt.client_message_id == "uuid-1"
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_timeout_after_successful_write_is_uncertain_and_never_rewritten_as_retryable() -> None:
    message_send = _protocol_module()
    router = RequestRouter()
    writes: list[dict[str, Any]] = []

    async def send_request(frame: dict[str, Any]) -> bool:
        writes.append(frame)
        return True

    with pytest.raises(message_send.MessageSendUncertain):
        await message_send.request_text_message(
            router,
            send_request,
            account_user_id="seller-1",
            chat_id="chat-9",
            receiver_id="buyer-2",
            text="hello",
            timeout_s=0.01,
            mid_factory=lambda: "mid-timeout",
            uuid_factory=lambda: "uuid-timeout",
        )

    assert len(writes) == 1
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_router_close_after_successful_write_is_uncertain() -> None:
    message_send = _protocol_module()
    router = RequestRouter()

    async def send_request(_frame: dict[str, Any]) -> bool:
        router.close()
        return True

    with pytest.raises(message_send.MessageSendUncertain) as caught:
        await message_send.request_text_message(
            router,
            send_request,
            account_user_id="seller-1",
            chat_id="chat-9",
            receiver_id="buyer-2",
            text="hello",
            timeout_s=0.1,
            mid_factory=lambda: "mid-close",
            uuid_factory=lambda: "uuid-close",
        )

    assert "cancelled after write" in str(caught.value)
    assert caught.value.request_id == "mid-close"
    assert caught.value.client_message_id == "uuid-close"
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_caller_cancellation_still_propagates() -> None:
    message_send = _protocol_module()
    router = RequestRouter()
    written = asyncio.Event()

    async def send_request(_frame: dict[str, Any]) -> bool:
        written.set()
        return True

    task = asyncio.create_task(
        message_send.request_text_message(
            router,
            send_request,
            account_user_id="seller-1",
            chat_id="chat-9",
            receiver_id="buyer-2",
            text="hello",
            timeout_s=None,
            mid_factory=lambda: "mid-cancel",
            uuid_factory=lambda: "uuid-cancel",
        )
    )
    await written.wait()
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_proven_pre_write_failure_is_retryable_without_claiming_platform_side_effect() -> None:
    message_send = _protocol_module()
    router = RequestRouter()

    async def send_request(_frame: dict[str, Any]) -> bool:
        return False

    with pytest.raises(message_send.MessageSendNotSent):
        await message_send.request_text_message(
            router,
            send_request,
            account_user_id="seller-1",
            chat_id="chat-9",
            receiver_id="buyer-2",
            text="hello",
            mid_factory=lambda: "mid-false",
            uuid_factory=lambda: "uuid-false",
        )

    assert router.pending_count == 0


@pytest.mark.asyncio
async def test_send_service_audits_attempt_before_write_and_does_not_blind_retry_uncertain() -> None:
    send_service = _application_module()
    order: list[str] = []
    calls: list[dict[str, str]] = []
    results: list[Any] = []

    class Recorder:
        async def record_attempt(self, **_kwargs: Any) -> None:
            order.append("attempt")

        async def record_result(self, **kwargs: Any) -> None:
            order.append("result")
            results.append(kwargs)

    class Protocol:
        async def send_text_message(self, **kwargs: str):
            order.append("protocol")
            calls.append(kwargs)
            raise send_service.SendMessageUncertain("response lost after write")

    service = send_service.SendMessageService(Protocol(), Recorder())
    result = await service.send_text(
        account_id="account-1",
        chat_id="chat-9",
        receiver_id="buyer-2",
        text="hello",
    )

    assert result.status is send_service.SendAttemptStatus.UNCERTAIN
    assert result.retry_allowed is False
    assert order == ["attempt", "protocol", "result"]
    assert len(calls) == 1
    assert calls[0] == {
        "chat_id": "chat-9",
        "receiver_id": "buyer-2",
        "text": "hello",
    }
    assert results[0]["status"] is send_service.SendAttemptStatus.UNCERTAIN


@pytest.mark.asyncio
async def test_platform_success_plus_result_audit_failure_requires_reconciliation() -> None:
    send_service = _application_module()
    protocol_calls = 0

    class Receipt:
        request_id = "mid-ok"
        client_message_id = "uuid-ok"
        response: ClassVar[dict[str, int]] = {"code": 200}

    class Recorder:
        async def record_attempt(self, **_kwargs: Any) -> None:
            return None

        async def record_result(self, **_kwargs: Any) -> None:
            raise RuntimeError("audit database unavailable")

    class Protocol:
        async def send_text_message(self, **_kwargs: str):
            nonlocal protocol_calls
            protocol_calls += 1
            return Receipt()

    service = send_service.SendMessageService(Protocol(), Recorder())
    result = await service.send_text(
        account_id="account-1",
        chat_id="chat-9",
        receiver_id="buyer-2",
        text="hello",
    )

    assert protocol_calls == 1
    assert result.status is send_service.SendAttemptStatus.RECONCILIATION_REQUIRED
    assert result.retry_allowed is False
    assert result.request_id == "mid-ok"
    assert result.client_message_id == "uuid-ok"


@pytest.mark.asyncio
async def test_platform_success_persistence_failure_redacts_message_text_from_terminal_audit() -> None:
    send_service = _application_module()
    secret_text = "sensitive-message-body-123"
    recorded_results: list[dict[str, Any]] = []

    class Receipt:
        request_id = "mid-redact"
        client_message_id = "uuid-redact"
        response: ClassVar[dict[str, Any]] = {
            "code": 200,
            "body": {"messageId": "platform-redact"},
        }

    class Recorder:
        async def record_attempt(self, **_kwargs: Any) -> None:
            return None

        async def record_result(self, **kwargs: Any) -> None:
            recorded_results.append(kwargs)

    class Protocol:
        async def send_text_message(self, **_kwargs: str):
            return Receipt()

    class MessageStore:
        async def resolve_receiver(self, **_kwargs: str) -> str | None:
            return "buyer-2"

        async def record_outbound(self, **_kwargs: Any) -> None:
            raise RuntimeError(f"database params contain {secret_text}")

    service = send_service.SendMessageService(Protocol(), Recorder(), MessageStore())
    result = await service.send_text(
        account_id="account-1",
        chat_id="chat-9",
        receiver_id="buyer-2",
        text=secret_text,
    )

    assert result.status is send_service.SendAttemptStatus.RECONCILIATION_REQUIRED
    assert result.retry_allowed is False
    assert result.detail == "outbound persistence failed after platform success: RuntimeError"
    assert secret_text not in (result.detail or "")
    assert len(recorded_results) == 1
    assert recorded_results[0]["detail"] == result.detail
    assert secret_text not in str(recorded_results[0])
