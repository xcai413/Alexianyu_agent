"""M4 与 daemon 同进程的 HTTP 健康探针。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from xianyu_agent.application.health.http import create_health_app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import DaemonInstance, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, daemon as daemon_domain
from xianyu_agent.domain.account import lease as account_lease
from xianyu_agent.protocol.client import ClientConfig, WsClient
from xianyu_agent.protocol.events import ConnectionState


@pytest.fixture
async def health_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "health.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _get(app: object) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://health.local",
    ) as client:
        return await client.get("/health")


async def test_health_endpoint_reports_db_heartbeat_and_owner_summary(health_db) -> None:
    _ = health_db
    await domain_accounts.create_account("acc-1")
    await domain_accounts.set_desired_state("acc-1", "running")
    assert await account_lease.claim("acc-1", instance_id="replica-a")
    client = WsClient("acc-1", config=ClientConfig(ws_url=""))
    await client._update_worker_status(state=ConnectionState.CONNECTED, detail=None)
    await daemon_domain.create_instance(instance_id="replica-a", pid=os.getpid(), version="test")
    await daemon_domain.mark_running("replica-a")

    response = await _get(create_health_app("replica-a"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "healthy"
    assert payload["database_writable"] is True
    assert payload["daemon"] == "online"
    assert payload["owned_accounts"] == 1
    assert payload["owned_drifted_accounts"] == 0


async def test_health_endpoint_returns_503_for_stale_self_heartbeat(health_db) -> None:
    _ = health_db
    row = await daemon_domain.create_instance(
        instance_id="replica-a", pid=os.getpid(), version="test"
    )
    await daemon_domain.mark_running(row.instance_id)
    async with db_mod.get_async_session() as session:
        persisted = await session.get(DaemonInstance, row.id)
        assert persisted is not None
        persisted.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=3)
        await session.commit()

    response = await _get(create_health_app("replica-a"))

    assert response.status_code == 503
    assert response.json()["daemon"] == "stale"
