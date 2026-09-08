"""Regression tests for the M5 WebSocket connector responsibility split."""

from __future__ import annotations

import sys
from types import ModuleType

from xianyu_agent.protocol import client
from xianyu_agent.protocol.ws import connector


def test_open_connection_preserves_transport_arguments(monkeypatch) -> None:
    sentinel = object()
    calls: list[tuple[str, dict]] = []
    fake_websockets = ModuleType("websockets")

    def fake_connect(url: str, **kwargs):
        calls.append((url, kwargs))
        return sentinel

    fake_websockets.connect = fake_connect
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)

    result = connector.open_connection("wss://example.invalid/ws", "cookie-value")

    assert result is sentinel
    assert calls == [
        (
            "wss://example.invalid/ws",
            {
                "additional_headers": [("Cookie", "cookie-value")],
                "ping_interval": None,
                "ping_timeout": None,
            },
        )
    ]


def test_ws_client_uses_canonical_connector_module() -> None:
    assert client.ws_connector is connector
