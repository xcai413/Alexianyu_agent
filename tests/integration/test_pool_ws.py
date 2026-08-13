"""Integration test: 3 accounts in one pool against a local mock WS server."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from cryptography.fernet import Fernet
from sqlalchemy import select

from tests.integration.ws_test_support import cache_test_ws_token, complete_test_registration
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Message, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.services.account_pool import AccountPool


class FakeServer:
    def __init__(self) -> None:
        self.connections: list[str] = []
        self._counter = 0

    async def handle(self, ws) -> None:
        self.connections.append(str(ws.remote_address))
        self._counter += 1
        try:
            await complete_test_registration(ws)
            # Push one message immediately, then read until close.
            await ws.send(
                json.dumps(
                    {
                        "code": 0,
                        "body": {
                            "bizType": "text",
                            "1": "pool 买家问价",
                            "2": "buyer-p",
                            "4": "text",
                            "6": {"mid": f"P-{self._counter}"},
                            "10": "chat-pool",
                        },
                    }
                )
            )
            async for _ in ws:
                pass
        except Exception:
            pass
        finally:
            await ws.close()


@pytest.fixture
async def fake_pool_server():
    server = FakeServer()
    async with websockets.serve(server.handle, "127.0.0.1", 0) as srv:
        host, port = srv.sockets[0].getsockname()[:2]
        yield f"ws://{host}:{port}", server


@pytest.mark.asyncio
async def test_pool_three_accounts_online(
    fake_pool_server, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, _server = fake_pool_server
    db = tmp_path / "pool.db"
    fernet_key = Fernet.generate_key().decode()
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    monkeypatch.setenv("XIANYU_FERNET_KEY", fernet_key)
    monkeypatch.setenv("XIANYU_WS_URL", url)
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    signer = CookieSigner()
    for i in range(3):
        acc = f"acc-{i}"
        await domain_accounts.create_account(acc, enabled=True)
        await signer.save_cookie(acc, f"unb={acc}; _m_h5_tk=seed_{i}_xyz; cookie2=abc")
        await cache_test_ws_token(acc, signer, token=f"test-token-{i}")

    pool = await AccountPool.from_enabled_accounts()
    started = pool.start_all()
    assert len(started) == 3

    # Give workers time to connect, receive, and persist.
    await asyncio.sleep(3.0)

    statuses = await pool.status()
    connected = [s for s in statuses if s["db_status"] == "connected"]
    assert len(connected) == 3, f"statuses={statuses}"
    for s in statuses:
        assert s["last_heartbeat_at"] is not None, f"no heartbeat for {s}"

    await pool.stop_all()

    async with get_async_session() as session:
        msgs = list((await session.execute(select(Message))).scalars().all())
    assert len(msgs) == 3, f"messages={len(msgs)}"
    assert all(m.chat_id == "chat-pool" for m in msgs)

    await db_mod.async_engine.dispose()
    reset_settings_cache()
