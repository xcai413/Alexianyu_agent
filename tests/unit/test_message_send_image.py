"""图片消息:与文本同源的信封,严格 https 校验与同样的失败分类。"""

from __future__ import annotations

import base64
import json

import pytest

from xianyu_agent.application.message import SendAttemptStatus, SendMessageService
from xianyu_agent.protocol.ws.message_send import (
    MessageSendRejected,
    MessageSendUncertain,
    build_image_message_request,
)


def _decoded_content(frame: dict) -> dict:
    encoded = frame["body"][0]["content"]["custom"]["data"]
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


def test_image_frame_matches_the_reference_shape() -> None:
    frame = build_image_message_request(
        account_user_id="seller-1",
        chat_id="chat-1",
        receiver_id="buyer-1",
        image_url="https://img.example/card.png",
        width=1024,
        height=768,
    )

    assert frame["lwp"] == "/r/MessageSend/sendByReceiverScope"
    content = _decoded_content(frame)
    assert content == {
        "contentType": 2,
        "image": {
            "pics": [
                {
                    "height": 768,
                    "type": 0,
                    "url": "https://img.example/card.png",
                    "width": 1024,
                }
            ]
        },
    }
    assert frame["body"][0]["content"]["contentType"] == 101
    assert frame["body"][1]["actualReceivers"] == ["buyer-1@goofish", "seller-1@goofish"]


def test_image_frame_defaults_to_reference_dimensions() -> None:
    frame = build_image_message_request(
        account_user_id="s",
        chat_id="c",
        receiver_id="r",
        image_url="https://img.example/a.png",
        width=0,
        height=0,
    )

    pic = _decoded_content(frame)["image"]["pics"][0]
    assert (pic["width"], pic["height"]) == (800, 600)


@pytest.mark.parametrize("url", ["http://img.example/a.png", "file:///tmp/a.png", "a.png"])
def test_image_frame_rejects_non_https_urls(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        build_image_message_request(
            account_user_id="s", chat_id="c", receiver_id="r", image_url=url
        )


class _Protocol:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def send_image_message(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return type(
            "Receipt", (), {"request_id": "mid-1", "client_message_id": "uuid-1", "response": {}}
        )()


class _Recorder:
    def __init__(self) -> None:
        self.attempts: list[dict[str, object]] = []
        self.results: list[object] = []

    async def record_attempt(self, **kwargs) -> None:
        self.attempts.append(kwargs)

    async def record_result(self, **kwargs) -> None:
        self.results.append(kwargs)


class _Store:
    def __init__(self) -> None:
        self.outbound: list[dict[str, object]] = []

    async def resolve_receiver(self, *, account_id: str, chat_id: str) -> str | None:
        return "buyer-1"

    async def record_outbound(self, **kwargs) -> None:
        self.outbound.append(kwargs)


@pytest.mark.asyncio
async def test_send_image_records_an_image_message_not_text() -> None:
    protocol = _Protocol()
    recorder = _Recorder()
    store = _Store()
    service = SendMessageService(protocol, recorder, store)  # type: ignore[arg-type]

    result = await service.send_image(
        account_id="acc-1",
        chat_id="chat-1",
        image_url="https://img.example/card.png",
    )

    assert result.status is SendAttemptStatus.SUCCESS
    assert protocol.calls[0]["image_url"] == "https://img.example/card.png"
    assert store.outbound[0]["content_type"] == "image"
    assert store.outbound[0]["text"] == "https://img.example/card.png"


@pytest.mark.asyncio
async def test_send_image_classifies_rejection_and_uncertainty() -> None:
    rejected = SendMessageService(  # type: ignore[arg-type]
        _Protocol(error=MessageSendRejected("code=500")), _Recorder(), _Store()
    )
    uncertain = SendMessageService(  # type: ignore[arg-type]
        _Protocol(error=MessageSendUncertain("timeout")), _Recorder(), _Store()
    )

    rejected_result = await rejected.send_image(
        account_id="acc-1", chat_id="chat-1", image_url="https://img.example/a.png"
    )
    uncertain_result = await uncertain.send_image(
        account_id="acc-1", chat_id="chat-1", image_url="https://img.example/a.png"
    )

    assert rejected_result.status is SendAttemptStatus.FAILED_FINAL
    assert rejected_result.retry_allowed is False
    assert uncertain_result.status is SendAttemptStatus.UNCERTAIN
    assert uncertain_result.retry_allowed is False


@pytest.mark.asyncio
async def test_send_image_without_a_resolvable_receiver_fails_before_write() -> None:
    class _EmptyStore(_Store):
        async def resolve_receiver(self, *, account_id: str, chat_id: str) -> str | None:
            return None

    protocol = _Protocol()
    service = SendMessageService(  # type: ignore[arg-type]
        protocol, _Recorder(), _EmptyStore()
    )

    result = await service.send_image(
        account_id="acc-1", chat_id="chat-1", image_url="https://img.example/a.png"
    )

    assert result.status is SendAttemptStatus.FAILED_FINAL
    assert protocol.calls == []
