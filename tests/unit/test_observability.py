"""P0.4 daemon 与账号统一运行快照测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.db.models import WorkerDesiredState
from xianyu_agent.domain import (
    accounts as domain_accounts,
    daemon as daemon_domain,
    worker_risk,
)
from xianyu_agent.domain.account import lease as account_lease
from xianyu_agent.protocol.client import ClientConfig, WsClient
from xianyu_agent.protocol.events import ConnectionState
from xianyu_agent.services import daemon_health
from xianyu_agent.services.observability import build_runtime_snapshot


@pytest.fixture
async def observability_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "observability.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_snapshot_reports_daemon_uptime_and_aligned_worker(
    observability_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await domain_accounts.create_account("a")
    await domain_accounts.set_desired_state("a", WorkerDesiredState.RUNNING)
    client = WsClient("a", config=ClientConfig(ws_url=""))
    await client._update_worker_status(state=ConnectionState.CONNECTED, detail=None)
    row = await daemon_domain.create_instance(instance_id="d1", pid=123, version="test")
    await daemon_domain.mark_running(row.instance_id)
    monkeypatch.setattr(daemon_health, "pid_alive", lambda _pid: True)

    snapshot = await build_runtime_snapshot(now=datetime.now(UTC) + timedelta(seconds=5))

    assert snapshot.daemon_health.healthy is True
    assert snapshot.daemon_uptime_s is not None
    assert snapshot.daemon_uptime_s >= 5
    assert snapshot.accounts[0].actual_state == "connected"
    assert snapshot.accounts[0].aligned is True
    assert snapshot.drifted_accounts == ()


@pytest.mark.asyncio
async def test_snapshot_marks_stale_worker_as_drift(
    observability_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await domain_accounts.create_account("a")
    await domain_accounts.set_desired_state("a", WorkerDesiredState.RUNNING)
    client = WsClient("a", config=ClientConfig(ws_url=""))
    await client._update_worker_status(state=ConnectionState.CONNECTED, detail=None)
    row = await domain_accounts.worker_status_for(account.account_id)
    assert row is not None
    async with db_mod.get_async_session() as session:
        persisted = await session.get(type(row), row.id)
        assert persisted is not None
        persisted.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=120)
        await session.commit()
    monkeypatch.setattr(daemon_health, "pid_alive", lambda _pid: False)

    snapshot = await build_runtime_snapshot()

    observed = snapshot.accounts[0]
    assert observed.actual_state == "stale"
    assert observed.aligned is False
    assert observed.issue == "Worker 心跳过期"
    assert "a: Worker 心跳过期" in snapshot.operational_alerts


@pytest.mark.asyncio
async def test_snapshot_flags_worker_running_when_stopped_is_expected(
    observability_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await domain_accounts.create_account("a")
    client = WsClient("a", config=ClientConfig(ws_url=""))
    await client._update_worker_status(state=ConnectionState.CONNECTED, detail=None)
    monkeypatch.setattr(daemon_health, "pid_alive", lambda _pid: False)

    snapshot = await build_runtime_snapshot()

    assert snapshot.accounts[0].aligned is False
    assert snapshot.accounts[0].issue == "期望 stopped,实际 connected"


@pytest.mark.asyncio
async def test_snapshot_reports_user_validate_cooldown_as_operational_alert(
    observability_db,
) -> None:
    _ = observability_db
    await domain_accounts.create_account("a")
    now = datetime(2026, 8, 21, 4, 0, tzinfo=UTC)
    await worker_risk.open_user_validate("a", now=now)

    # 首次命中按最短档冷却(60 秒),因此在冷却窗口内取样。
    snapshot = await build_runtime_snapshot(now=now + timedelta(seconds=50))

    observed = snapshot.accounts[0]
    assert observed.actual_state == "risk_cooling"
    assert observed.aligned is True
    assert observed.risk_code == "FAIL_SYS_USER_VALIDATE"
    assert observed.risk_status == "FAIL_SYS_USER_VALIDATE 验证冷却 00:00:10"
    assert "a: FAIL_SYS_USER_VALIDATE 验证冷却 00:00:10" in snapshot.operational_alerts


@pytest.mark.asyncio
async def test_snapshot_lists_multiple_replicas_and_account_owners(observability_db) -> None:
    _ = observability_db
    for account_id, instance_id in (("a", "replica-a"), ("b", "replica-b")):
        await domain_accounts.create_account(account_id)
        await domain_accounts.set_desired_state(account_id, WorkerDesiredState.RUNNING)
        client = WsClient(account_id, config=ClientConfig(ws_url=""))
        await client._update_worker_status(state=ConnectionState.CONNECTED, detail=None)
        assert await account_lease.claim(account_id, instance_id=instance_id)
        row = await daemon_domain.create_instance(instance_id=instance_id, pid=1, version="test")
        await daemon_domain.mark_running(row.instance_id)

    snapshot = await build_runtime_snapshot()

    assert len(snapshot.daemon_instances) == 2
    assert len(snapshot.healthy_daemons) == 2
    owners = {row.account_id: row.owner_instance_id for row in snapshot.accounts}
    assert owners == {"a": "replica-a", "b": "replica-b"}
    by_instance = {
        row.instance.instance_id: row.owned_accounts for row in snapshot.daemon_instances
    }
    assert by_instance == {"replica-a": ("a",), "replica-b": ("b",)}
