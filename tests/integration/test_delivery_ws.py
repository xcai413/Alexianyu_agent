"""Integration test: OrderPaid over mock WS triggers auto-delivery."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from cryptography.fernet import Fernet
from sqlalchemy import select

from tests.integration.ws_test_support import cache_test_ws_token
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import (
    Card,
    CardConsumption,
    Order,
    database as db_mod,
    get_async_session,
)
from xianyu_agent.db.models import OrderStatus
from xianyu_agent.domain import accounts as domain_accounts, cards as domain_cards
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.services.account_pool import AccountPool


class DeliveryServer:
    def __init__(self) -> None:
        self.received: list[str] = []

    async def handle(self, ws) -> None:
        try:
            # Push a paid-order frame; then capture whatever the client sends.
            await ws.send(
                json.dumps(
                    {
                        "code": 0,
                        "body": {
                            "bizType": "order",
                            "5": {
                                "orderId": "D-1",
                                "buyerId": "buyer-d",
                                "amount": 5.0,
                                "status": "paid",
                            },
                            "100": {"itemId": "I-1", "title": "虚拟商品"},
                            "6": {"time": 1700000000000},
                        },
                    }
                )
            )
            async for msg in ws:
                self.received.append(msg)
        except Exception:
            pass
        finally:
            await ws.close()


@pytest.fixture
async def delivery_server():
    server = DeliveryServer()
    async with websockets.serve(server.handle, "127.0.0.1", 0) as srv:
        host, port = srv.sockets[0].getsockname()[:2]
        yield f"ws://{host}:{port}", server


@pytest.mark.asyncio
async def test_order_paid_triggers_delivery(
    delivery_server, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, server = delivery_server
    db = tmp_path / "delivery.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("XIANYU_WS_URL", url)
    monkeypatch.setenv("XIANYU_AUTOMATION_MODE", "active")
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    await domain_accounts.create_account("acc-d", enabled=True)
    signer = CookieSigner()
    await signer.save_cookie("acc-d", "unb=acc-d; _m_h5_tk=seed_d_xyz; cookie2=abc")
    await cache_test_ws_token("acc-d", signer)
    card = await domain_cards.create_card("acc-d", "虚拟卡", "DEL-CODE-1\nDEL-CODE-2", type_="text")

    pool = await AccountPool.from_enabled_accounts()
    pool.start_all()
    await asyncio.sleep(3.0)
    await pool.stop_all()

    async with get_async_session() as session:
        orders = list((await session.execute(select(Order))).scalars().all())
        cons = list((await session.execute(select(CardConsumption))).scalars().all())
        card_row = (
            await session.execute(select(Card).where(Card.id == card.id).limit(1))
        ).scalar_one()

    assert len(orders) == 1
    assert orders[0].order_id == "D-1"
    assert orders[0].status == OrderStatus.DELIVERED.value
    assert orders[0].delivery_content == "DEL-CODE-1"
    assert len(cons) == 1
    assert cons[0].content == "DEL-CODE-1"
    assert cons[0].status == "success"
    assert card_row.remaining == 1
    # The mock server must have received the delivered code.
    assert any("DEL-CODE-1" in m for m in server.received), f"received={server.received}"

    await db_mod.async_engine.dispose()
    reset_settings_cache()
