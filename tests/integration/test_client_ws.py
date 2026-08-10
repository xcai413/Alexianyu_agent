"""Integration test: WsClient against a local mock WS server."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from cryptography.fernet import Fernet
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, Message, Order, database as db_mod, get_async_session
from xianyu_agent.db.models import OrderStatus
from xianyu_agent.domain import messages as dm, orders as do
from xianyu_agent.protocol.client import ClientConfig, WsClient
from xianyu_agent.protocol.events import ConnectionState, MessageReceived, OrderPaid
from xianyu_agent.protocol.signer import CookieSigner


class FakeServer:
    def __init__(self, frames):
        self.frames = frames
        self.received = []

    async def handle(self, ws):
        try:
            for f in self.frames:
                await ws.send(json.dumps(f))
            async for msg in ws:
                self.received.append(msg)
        except Exception:
            pass
        finally:
            await ws.close()


@pytest.fixture
async def fake_ws_server():
    frames = [
        {
            "code": 0,
            "body": {
                "bizType": "text",
                "1": "买家问价",
                "2": "b-1",
                "3": "s-1",
                "4": "text",
                "6": {"mid": "INT-1"},
                "10": "chat-int",
            },
        },
        {
            "code": 0,
            "body": {
                "bizType": "order",
                "5": {"orderId": "INT-O-1", "buyerId": "b-1", "amount": 5.0, "status": "paid"},
            },
        },
    ]
    server = FakeServer(frames)
    async with websockets.serve(server.handle, "127.0.0.1", 0) as srv:
        host, port = srv.sockets[0].getsockname()[:2]
        url = f"ws://{host}:{port}"
        yield url, server


@pytest.mark.asyncio
async def test_client_connects_parses_and_persists(fake_ws_server, tmp_path, monkeypatch) -> None:
    url, _server = fake_ws_server
    db = tmp_path / "intg.db"
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    monkeypatch.setenv("XIANYU_FERNET_KEY", fernet_key)
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    async with get_async_session() as session:
        session.add(Account(account_id="s-1", enabled=True))
        await session.commit()
    signer = CookieSigner()
    await signer.save_cookie("s-1", "unb=s-1; _m_h5_tk=fake_seed_token_xyz; cookie2=abc")
    received_events = []

    async def on_event(ev):
        received_events.append(ev)
        if isinstance(ev, MessageReceived):
            await dm.upsert_inbound(ev)
        elif hasattr(ev, "order_id"):
            await do.upsert_from_event(ev)

    client = WsClient(
        "s-1",
        on_event=on_event,
        config=ClientConfig(
            ws_url=url, heartbeat_interval_s=1.0, min_backoff_s=0.1, max_backoff_s=1.0
        ),
    )
    client.start()
    await asyncio.sleep(2.5)
    await client.stop()
    assert client.state in {
        ConnectionState.DISCONNECTED,
        ConnectionState.ERROR,
        ConnectionState.CONNECTED,
    }
    assert any(isinstance(e, MessageReceived) for e in received_events), f"events={received_events}"
    assert any(isinstance(e, OrderPaid) for e in received_events), f"events={received_events}"
    async with get_async_session() as session:
        msgs = list((await session.execute(select(Message))).scalars().all())
        orders = list((await session.execute(select(Order))).scalars().all())
    assert any(m.chat_id == "chat-int" for m in msgs)
    assert len(orders) == 1
    assert orders[0].order_id == "INT-O-1"
    assert orders[0].status == OrderStatus.PAID.value
    await db_mod.async_engine.dispose()
    reset_settings_cache()
