"""Inbound WebSocket frame decoding boundary."""

from __future__ import annotations

import json

from xianyu_agent.protocol.events import WsFrame


class FrameDecoder:
    """Decode transport payloads into protocol frames without side effects."""

    def decode(self, raw: str | bytes) -> WsFrame | None:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return WsFrame.model_validate(data) if isinstance(data, dict) else WsFrame(body=text)
