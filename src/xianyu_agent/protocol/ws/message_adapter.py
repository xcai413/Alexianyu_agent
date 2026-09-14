"""Business-message adapter over an already-active canonical WsClient session."""

from __future__ import annotations

import json
from typing import Any

from xianyu_agent.protocol.ws import message_send


class WsClientMessageProtocol:
    """Expose calibrated chat sending while keeping seller identity in protocol space."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def send_text_message(
        self,
        *,
        chat_id: str,
        receiver_id: str,
        text: str,
    ) -> message_send.MessageSendReceipt:
        ws, router = self._client._require_protocol_context()
        account_user_id = getattr(self._client, "_account_user_id", None)
        if not isinstance(account_user_id, str) or not account_user_id.strip():
            raise ConnectionError("WebSocket account user id is not available")

        async def send_request(frame: message_send.MessageSendFrame) -> bool:
            await ws.send(json.dumps(frame))
            return True

        return await message_send.request_text_message(
            router,
            send_request,
            account_user_id=account_user_id,
            chat_id=chat_id,
            receiver_id=receiver_id,
            text=text,
        )
