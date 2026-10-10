"""Regression: a cancelled heartbeat touch must still finish its DB write.

aiosqlite resolves an in-flight call on its worker thread. If cancellation
abandons the await mid-flight, that thread finishes against a closing event
loop and raises there, which pytest reports against whatever test happens to be
running at the time.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, WorkerStatus, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.ws import client as ws_client_module


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "heartbeat.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_cancelled_heartbeat_touch_still_persists(clean_db, monkeypatch) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    async with db_mod.get_async_session() as session:
        account = (await session.execute(select(Account))).scalar_one()
        session.add(WorkerStatus(account_id=account.id, status="connecting"))
        await session.commit()

    client = ws_client_module.WsClient("acc-1", signer=object(), token_provider=object())
    write_started = asyncio.Event()
    write_gate = asyncio.Event()
    real_get_async_session = ws_client_module.get_async_session

    @asynccontextmanager
    async def gated_session():
        write_started.set()
        await write_gate.wait()
        async with real_get_async_session() as session:
            yield session

    monkeypatch.setattr(ws_client_module, "get_async_session", gated_session)

    touch = asyncio.create_task(client._touch_heartbeat())
    await asyncio.wait_for(write_started.wait(), timeout=1)
    touch.cancel()
    write_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await touch

    async with db_mod.get_async_session() as session:
        row = (await session.execute(select(WorkerStatus))).scalar_one()
    assert row.last_heartbeat_at is not None
