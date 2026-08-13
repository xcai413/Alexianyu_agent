"""Reconnect replay must remain idempotent in durable SQLite state."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
import websockets
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from tests.integration.ws_test_support import cache_test_ws_token, complete_test_registration
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Message, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, messages as domain_messages
from xianyu_agent.protocol.client import ClientConfig, WsClient
from xianyu_agent.protocol.events import MessageReceived
from xianyu_agent.protocol.signer import CookieSigner


def _replayed_push() -> dict:
    payload = {
        "1": {
            "2": "replay-chat@goofish",
            "5": 1700000000000,
            "10": {
                "senderUserId": "replay-buyer",
                "reminderContent": "same replayed message",
                "bizTag": '{"messageId":"REPLAY-MSG-1","itemId":"REPLAY-ITEM-1"}',
            },
        }
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return {
        "code": 200,
        "headers": {"mid": "push-replay", "sid": "session-replay"},
        "body": {"syncPushPackage": {"data": [{"data": encoded}]}},
    }


class ReplayServer:
    def __init__(self) -> None:
        self.connections = 0

    async def handle(self, ws) -> None:
        self.connections += 1
        await complete_test_registration(ws)
        await ws.send(json.dumps(_replayed_push()))
        await ws.recv()  # client ACK
        await ws.close()


@pytest.mark.asyncio
async def test_reconnect_replay_persists_one_message(tmp_path, monkeypatch) -> None:
    server = ReplayServer()
    async with websockets.serve(server.handle, "127.0.0.1", 0) as socket_server:
        host, port = socket_server.sockets[0].getsockname()[:2]
        url = f"ws://{host}:{port}"
        monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "replay.db"))
        monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
        reset_settings_cache()
        db_mod.reset_engine()
        await db_mod.init_db()
        await domain_accounts.create_account("replay-account", enabled=True)
        signer = CookieSigner()
        await signer.save_cookie(
            "replay-account", "unb=replay-seller; _m_h5_tk=replay_seed; cookie2=c2"
        )
        await cache_test_ws_token("replay-account", signer)

        async def on_event(event) -> None:
            if isinstance(event, MessageReceived):
                await domain_messages.upsert_inbound(event.model_copy(update={"raw": None}))

        client = WsClient(
            "replay-account",
            signer=signer,
            on_event=on_event,
            config=ClientConfig(
                ws_url=url,
                heartbeat_interval_s=10,
                registration_delay_s=0,
                min_backoff_s=0.01,
                max_backoff_s=0.02,
            ),
        )
        client.start()
        try:
            for _ in range(100):
                if server.connections >= 2:
                    async with get_async_session() as session:
                        count = await session.scalar(select(func.count()).select_from(Message))
                    if count == 1:
                        break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("client did not reconnect and replay")
        finally:
            await client.stop()

        async with get_async_session() as session:
            rows = list((await session.execute(select(Message))).scalars())
        assert server.connections >= 2
        assert len(rows) == 1
        assert rows[0].message_id == "REPLAY-MSG-1"
        await db_mod.async_engine.dispose()
        reset_settings_cache()
