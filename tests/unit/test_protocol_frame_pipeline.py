"""Unit contracts for decomposed WS frame pipeline responsibilities."""

from __future__ import annotations

import json

import pytest

from xianyu_agent.protocol.ack import AckSender
from xianyu_agent.protocol.decoder import FrameDecoder
from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.sender import SocketSender


class FakeSocket:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    async def send(self, value: str) -> None:
        if self.fail:
            raise RuntimeError("transport failed")
        self.sent.append(value)


def test_frame_decoder_handles_text_bytes_and_invalid_json() -> None:
    decoder = FrameDecoder()
    text = decoder.decode('{"code": 200, "headers": {"mid": "m1"}}')
    binary = decoder.decode(b'{"body": {"bizType": "text"}}')

    assert text is not None
    assert text.code == 200
    assert text.headers == {"mid": "m1"}
    assert binary is not None
    assert binary.body == {"bizType": "text"}
    assert decoder.decode("not-json") is None


@pytest.mark.asyncio
async def test_ack_sender_preserves_ack_contract() -> None:
    socket = FakeSocket()
    frame = WsFrame(headers={"mid": "m1", "sid": "s1", "app-key": "app"})

    assert await AckSender().acknowledge(socket, frame) is True
    assert json.loads(socket.sent[0]) == {
        "code": 200,
        "headers": {"mid": "m1", "sid": "s1", "app-key": "app"},
    }
    assert await AckSender().acknowledge(socket, WsFrame(headers={})) is False
    assert await AckSender().acknowledge(None, frame) is False


@pytest.mark.asyncio
async def test_socket_sender_preserves_legacy_boolean_contract() -> None:
    sender = SocketSender()
    socket = FakeSocket()

    assert await sender.send_text(socket, "hello") is True
    assert socket.sent == ["hello"]
    assert await sender.send_text(None, "offline") is False
    assert await sender.send_text(FakeSocket(fail=True), "failure") is False
