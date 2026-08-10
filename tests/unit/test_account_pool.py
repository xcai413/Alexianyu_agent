"""Unit tests for AccountPool semantics and domain account services."""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.protocol.events import ConnectionState
from xianyu_agent.services.account_pool import AccountPool


class FakeWorker:
    """Minimal AccountWorker stand-in for pool semantics tests."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.state = ConnectionState.IDLE
        self.started_at = None
        self.stopped = 0

    def start(self) -> None:
        self.state = ConnectionState.CONNECTED
        self.started_at = "now"

    async def stop(self) -> None:
        self.state = ConnectionState.DISCONNECTED
        self.stopped += 1
        self.started_at = None


@pytest.mark.asyncio
async def test_pool_start_stop_semantics() -> None:
    pool = AccountPool()
    pool._workers = {
        "a": FakeWorker("a"),
        "b": FakeWorker("b"),
        "c": FakeWorker("c"),
    }
    assert pool.account_ids == ["a", "b", "c"]
    assert pool.has("a")
    assert not pool.has("zzz")

    # start one
    assert pool.start("a") is True
    assert pool.start("zzz") is False
    assert pool.get("a").state == ConnectionState.CONNECTED  # type: ignore[union-attr]
    assert pool.get("b").state == ConnectionState.IDLE  # type: ignore[union-attr]

    # start all
    started = pool.start_all()
    assert set(started) == {"a", "b", "c"}
    assert all(pool.get(x).state == ConnectionState.CONNECTED for x in pool.account_ids)  # type: ignore[union-attr]

    # stop one
    assert await pool.stop("b") is True
    assert await pool.stop("zzz") is False
    assert pool.get("b").stopped == 1  # type: ignore[union-attr]

    # stop all
    await pool.stop_all()
    assert all(w.stopped >= 1 for w in pool._workers.values())


@pytest.mark.asyncio
async def test_domain_accounts_crud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "acc.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    row = await domain_accounts.create_account("acc-1", nickname="n1", remark="r1")
    assert row.id is not None
    # idempotent create
    again = await domain_accounts.create_account("acc-1")
    assert again.id == row.id

    assert await domain_accounts.get_account("acc-1") is not None
    assert await domain_accounts.get_account("missing") is None

    rows = await domain_accounts.list_accounts()
    assert [r.account_id for r in rows] == ["acc-1"]
    enabled = await domain_accounts.list_accounts(only_enabled=True)
    assert len(enabled) == 1

    assert await domain_accounts.set_enabled("acc-1", False) is True
    assert await domain_accounts.set_enabled("missing", False) is False
    assert len(await domain_accounts.list_accounts(only_enabled=True)) == 0

    await domain_accounts.mark_worker_offline("acc-1", reason="test")
    ws = await domain_accounts.worker_status_for("acc-1")
    assert ws is not None
    assert ws.status == "offline"

    statuses = await domain_accounts.worker_statuses()
    assert len(statuses) == 1

    assert await domain_accounts.delete_account("acc-1") is True
    assert await domain_accounts.delete_account("missing") is False
    assert await domain_accounts.get_account("acc-1") is None

    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_pool_from_enabled_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "pool.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()

    await domain_accounts.create_account("on-1", enabled=True)
    await domain_accounts.create_account("on-2", enabled=True)
    await domain_accounts.create_account("off-1", enabled=False)

    pool = await AccountPool.from_enabled_accounts()
    assert set(pool.account_ids) == {"on-1", "on-2"}

    await db_mod.async_engine.dispose()
    reset_settings_cache()
