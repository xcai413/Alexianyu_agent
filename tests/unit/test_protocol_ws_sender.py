"""Regression tests for the M5 WebSocket outbound sender responsibility split."""

import pytest

from xianyu_agent.protocol import client
from xianyu_agent.protocol.ws import sender


class _Socket:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_send_text_success_preserves_payload() -> None:
    socket = _Socket()

    sent = await sender.send_text(socket, "hello")

    assert sent is True
    assert socket.sent == ["hello"]


@pytest.mark.asyncio
async def test_send_text_returns_false_without_socket() -> None:
    assert await sender.send_text(None, "hello") is False


@pytest.mark.asyncio
async def test_send_text_preserves_false_on_send_error_contract() -> None:
    socket = _Socket(fail=True)

    assert await sender.send_text(socket, "hello") is False
    assert socket.sent == []


def test_ws_client_uses_canonical_sender_module() -> None:
    assert client.ws_sender is sender
