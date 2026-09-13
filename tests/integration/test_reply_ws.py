"""Integration test: inbound message triggers rule auto-reply over mock WS."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from cryptography.fernet import Fernet
from sqlalchemy import select

from tests.integration.ws_test_support import (
    acknowledge_text_message,
    cache_test_ws_token,
    complete_test_registration,
    decode_outbound_text,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Message, ReplyLog, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts, rules as domain_rules
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.services.account_pool import AccountPool


class ReplyServer:
    def __init__(self) -> None:
        self.received: list[str] = []

    async def handle(self, ws) -> None:
        try:
            self.received.extend(await complete_test_registration(ws))
            await ws.send(
                json.dumps(
                    {
                        "code": 0,
                        "body": {
                            "bizType": "text",
                            "1": "你好,在吗?",
                            "2": "buyer-r",
                            "3": "acc-r",
                            "4": "text",
                            "6": {"mid": "R-1"},
                            "10": "chat-r",
                        },
                    }
                )
            )
            async for msg in ws:
                self.received.append(msg)
                await acknowledge_text_message(ws, msg)
        except Exception:
            pass
        finally:
            await ws.close()


@pytest.fixture
async def reply_server():
    server = ReplyServer()
    async with websockets.serve(server.handle, "127.0.0.1", 0) as srv:
        host, port = srv.sockets[0].getsockname()[:2]
        yield f"ws://{host}:{port}", server


@pytest.mark.asyncio
async def test_auto_reply_roundtrip(
    reply_server, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, server = reply_server
    db = tmp_path / "reply.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("XIANYU_WS_URL", url)
    monkeypatch.setenv("XIANYU_AUTOMATION_MODE", "active")
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    await domain_accounts.create_account("acc-r", enabled=True)
    signer = CookieSigner()
    await signer.save_cookie("acc-r", "unb=acc-r; _m_h5_tk=seed_r_xyz; cookie2=abc")
    await cache_test_ws_token("acc-r", signer)
    await domain_rules.create_rule(
        "在吗规则",
        "keyword",
        "在吗",
        "在的,亲,请问需要什么?",
        account_id="acc-r",
        priority=10,
    )
    await domain_rules.create_rule(
        "默认规则",
        "default",
        "",
        "您好,请问需要什么帮助?",
        account_id=None,
        priority=999,
    )

    pool = await AccountPool.from_enabled_accounts()
    pool.start_all()
    await asyncio.sleep(3.0)
    await pool.stop_all()

    async with get_async_session() as session:
        msgs = list((await session.execute(select(Message))).scalars().all())
        logs = list((await session.execute(select(ReplyLog))).scalars().all())
    assert len(msgs) == 2
    assert any(msg.content == "你好,在吗?" for msg in msgs)
    assert any(msg.content == "在的,亲,请问需要什么?" for msg in msgs)
    assert len(logs) == 1
    assert logs[0].success is True
    assert logs[0].sent_text == "在的,亲,请问需要什么?"
    assert logs[0].source == "rule"
    assert any(
        decode_outbound_text(message) == "在的,亲,请问需要什么?" for message in server.received
    ), f"received={server.received}"

    await db_mod.async_engine.dispose()
    reset_settings_cache()
