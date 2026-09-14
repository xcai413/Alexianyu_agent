"""Protocol-adapter contracts for Phase 3 outbound text messages."""

from __future__ import annotations

import importlib
import json

import pytest

from xianyu_agent.protocol.events import WsFrame
from xianyu_agent.protocol.ws.request_router import RequestRouter


@pytest.mark.asyncio
async def test_ws_client_adapter_keeps_seller_identity_inside_protocol_boundary() -> None:
    adapter_module = importlib.import_module("xianyu_agent.protocol.ws.message_adapter")
    router = RequestRouter()
    sent: list[dict] = []

    class Socket:
        async def send(self, payload: str) -> None:
            frame = json.loads(payload)
            sent.append(frame)
            assert router.match_frame(
                WsFrame(headers={"mid": frame["headers"]["mid"]}),
                {"code": 200, "body": {"messageId": "platform-1"}},
            )

    class Client:
        _account_user_id = "seller-1"

        def _require_protocol_context(self):
            return Socket(), router

    adapter = adapter_module.WsClientMessageProtocol(Client())
    receipt = await adapter.send_text_message(
        chat_id="chat-9",
        receiver_id="buyer-2",
        text="hello",
    )

    assert receipt.response["code"] == 200
    assert sent[0]["body"][0]["cid"] == "chat-9@goofish"
    assert sent[0]["body"][1]["actualReceivers"] == [
        "buyer-2@goofish",
        "seller-1@goofish",
    ]


@pytest.mark.asyncio
async def test_ws_client_adapter_fails_before_write_without_seller_identity() -> None:
    adapter_module = importlib.import_module("xianyu_agent.protocol.ws.message_adapter")
    router = RequestRouter()

    class Client:
        _account_user_id = None

        def _require_protocol_context(self):
            return object(), router

    adapter = adapter_module.WsClientMessageProtocol(Client())
    with pytest.raises(ConnectionError, match="account user id"):
        await adapter.send_text_message(
            chat_id="chat-9",
            receiver_id="buyer-2",
            text="hello",
        )

    assert router.pending_count == 0
