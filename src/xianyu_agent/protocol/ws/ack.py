"""WebSocket ACK sending responsibility."""

from __future__ import annotations

import json
from typing import Any

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.parser import build_ack_frame


async def send_ack(ws: Any | None, frame: WsFrame) -> bool:
    """Send the existing protocol ACK when one is required and a socket exists."""
    ack = build_ack_frame(frame)
    if ack is None or ws is None:
        return False
    await ws.send(json.dumps(ack))
    return True
