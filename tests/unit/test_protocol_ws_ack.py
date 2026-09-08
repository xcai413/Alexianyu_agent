"""Regression tests for the M5 WebSocket ACK responsibility split."""

import json

import pytest

from xianyu_agent.protocol import client, parser
from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws import ack


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def test_build_ack_frame_preserves_correlation_contract() -> None:
    frame = WsFrame(
        headers={"mid": "m-1", "sid": "s-1", "app-key": "app", "ua": "ua", "dt": "j"}
    )

    assert ack.build_ack_frame(frame) == {
        "code": 200,
        "headers": {
            "mid": "m-1",
            "sid": "s-1",
            "app-key": "app",
            "ua": "ua",
            "dt": "j",
        },
    }
    assert ack.build_ack_frame(WsFrame(headers={})) is None


def test_build_ack_frame_preserves_fallback_mid_hook() -> None:
    frame = WsFrame(headers={"sid": "s-2"})

    assert ack.build_ack_frame(frame, mid_factory=lambda: "fallback-mid") == {
        "code": 200,
        "headers": {"mid": "fallback-mid", "sid": "s-2"},
    }


def test_parser_ack_builder_delegates_and_preserves_legacy_mid_hook(monkeypatch) -> None:
    monkeypatch.setattr(parser, "_fallback_mid", lambda: "legacy-mid")

    assert parser.build_ack_frame(WsFrame(headers={"sid": "legacy-sid"})) == {
        "code": 200,
        "headers": {"mid": "legacy-mid", "sid": "legacy-sid"},
    }
    assert parser.ws_ack is ack


def test_parser_fallback_mid_alias_starts_canonical() -> None:
    assert parser._fallback_mid is ack.fallback_mid


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
