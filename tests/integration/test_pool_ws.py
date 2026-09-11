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
from xianyu_agent.protocol.ws.sync import ACK_DIFF_LWP, GET_STATE_LWP
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
            await self._complete_subscription_ready(ws)
            # Push one message only after the canonical subscription-ready exchange.
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

    async def _complete_subscription_ready(self, ws) -> None:
        """Drive the canonical syncExtra -> getState -> ackDiff readiness exchange."""
        await ws.send(
            json.dumps(
                {
                    "code": 0,
                    "headers": {
                        "mid": f"sync-extra-{self._counter}",
                        "sid": "test-session",
                    },
                    "body": {"syncExtraType": {"type": 1}},
                }
            )
        )

        get_state = await self._recv_request(ws, GET_STATE_LWP)
        await ws.send(
            json.dumps(
                {
                    "code": 200,
                    "headers": {
                        "mid": get_state["headers"]["mid"],
                        "sid": "test-session",
                    },
                    "body": {"topic": "sync", "pts": self._counter},
                }
            )
        )

        ack_diff = await self._recv_request(ws, ACK_DIFF_LWP)
        await ws.send(
            json.dumps(
                {
                    "code": 200,
                    "headers": {
                        "mid": ack_diff["headers"]["mid"],
                        "sid": "test-session",
                    },
                }
            )
        )

    @staticmethod
    async def _recv_request(ws, expected_lwp: str) -> dict:
        """Ignore protocol ACKs until the expected request arrives."""
        while True:
            payload = json.loads(await ws.recv())
            if payload.get("lwp") == expected_lwp:
                return payload


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

    # Give workers time to connect, finish SubscriptionReady, receive, and persist.
    await asyncio.sleep(3.0)

    statuses = await pool.status()
    online = [
        s
        for s in statuses
        if s["worker_state"] == "online" and s["db_status"] == "online"
    ]
    assert len(online) == 3, f"statuses={statuses}"
    for s in statuses:
        assert s["last_heartbeat_at"] is not None, f"no heartbeat for {s}"

    await pool.stop_all()

    async with get_async_session() as session:
        msgs = list((await session.execute(select(Message))).scalars().all())
    assert len(msgs) == 3, f"messages={len(msgs)}"
    assert all(m.chat_id == "chat-pool" for m in msgs)

    await db_mod.async_engine.dispose()
    reset_settings_cache()
