"""Regression tests for the M5 WebSocket heartbeat frame builder split."""

import json

from xianyu_agent.protocol import client
from xianyu_agent.protocol.ws import heartbeat


def test_generate_mid_preserves_legacy_format(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat.random, "random", lambda: 0.123)
    monkeypatch.setattr(heartbeat.time, "time", lambda: 1_700_000_000.123)

    assert heartbeat.generate_mid() == "1231700000000123 0"


def test_default_heartbeat_preserves_wire_frame(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat, "generate_mid", lambda: "mid-1")

    payload = json.loads(heartbeat.default_heartbeat())

    assert payload == {"lwp": "/!", "headers": {"mid": "mid-1"}}


def test_ws_client_keeps_compatibility_aliases_and_default_builder() -> None:
    assert client.ws_heartbeat is heartbeat
    assert client._generate_mid is heartbeat.generate_mid
    assert client._default_heartbeat is heartbeat.default_heartbeat
    assert client.ClientConfig.__dataclass_fields__["heartbeat_builder"].default is heartbeat.default_heartbeat
