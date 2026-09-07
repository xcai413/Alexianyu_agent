"""WebSocket ACK construction and transport boundary."""

from __future__ import annotations

import json
from typing import Any

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.parser import build_ack_frame


class AckSender:
    """Send correlation-preserving ACKs when a frame requires one."""

    async def acknowledge(self, socket: Any | None, frame: WsFrame) -> bool:
        ack = build_ack_frame(frame)
        if ack is None or socket is None:
            return False
        await socket.send(json.dumps(ack))
        return True
