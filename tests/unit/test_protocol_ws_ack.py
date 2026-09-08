"""Regression tests for the M5 WebSocket ACK responsibility split."""

import json

import pytest

from xianyu_agent.protocol import client
from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws import ack


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_send_ack_preserves_existing_wire_frame() -> None:
    socket = _Socket()
    frame = WsFrame(headers={"mid": "m-1", "sid": "s-1", "app-key": "app"})

    sent = await ack.send_ack(socket, frame)

    assert sent is True
    assert socket.sent == [
        json.dumps(
            {
                "code": 200,
                "headers": {"mid": "m-1", "sid": "s-1", "app-key": "app"},
            }
        )
    ]


@pytest.mark.asyncio
async def test_send_ack_skips_when_not_required_or_socket_missing() -> None:
    socket = _Socket()
    assert await ack.send_ack(socket, WsFrame(headers={})) is False
    assert socket.sent == []

    frame = WsFrame(headers={"mid": "m-2"})
    assert await ack.send_ack(None, frame) is False


def test_ws_client_uses_canonical_ack_module() -> None:
    assert client.ws_ack is ack
