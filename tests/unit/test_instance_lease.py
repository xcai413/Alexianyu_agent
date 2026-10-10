"""M1:实例租约与命令定向清理(多副本不再互相误判)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import DaemonInstance, database as db_mod
from xianyu_agent.db.models import WorkerCommandStatus
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.runtime import command as worker_commands, daemon as daemon_domain


@pytest.fixture
async def lease_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "lease.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode("ascii"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_instance(
    *,
    instance_id: str,
    lease_until: datetime | None,
    heartbeat_at: datetime,
    status: str = "running",
) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        session.add(
            DaemonInstance(
                instance_id=instance_id,
                pid=4242,
                status=status,
                version="0.1.0",
                started_at=heartbeat_at,
                last_heartbeat_at=heartbeat_at,
                lease_until=lease_until,
            )
        )


async def test_create_instance_gets_lease_and_heartbeat_renews_it(lease_db: Path) -> None:
    created = await daemon_domain.create_instance(instance_id="i1", pid=1, version="0.1.0")

    assert created.lease_until is not None
    assert created.lease_until > created.last_heartbeat_at

    control = await daemon_domain.touch_heartbeat("i1")

    assert control.shutdown_requested is False
    renewed = await daemon_domain.get_instance("i1")
    assert renewed is not None
    assert renewed.lease_until is not None
    assert renewed.lease_until >= created.lease_until


async def test_mark_expired_instances_only_touches_expired_rows(lease_db: Path) -> None:
    now = datetime.now(UTC)
    await daemon_domain.create_instance(instance_id="i-live", pid=1, version="0.1.0")
    await _seed_instance(
        instance_id="i-dead",
        lease_until=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(minutes=5),
    )
    # 升级前的旧行没有 lease_until:心跳还新就不动它,心跳也旧了才算异常退出。
    await _seed_instance(instance_id="i-legacy-live", lease_until=None, heartbeat_at=now)
    await _seed_instance(
        instance_id="i-legacy-stale",
        lease_until=None,
        heartbeat_at=now - timedelta(minutes=5),
    )

    marked = await daemon_domain.mark_expired_instances(reason="test cleanup", now=now)

    assert marked == 2
    # create_instance 落的是 starting;关键是被清理的只有过期/心跳也旧的行。
    assert (await daemon_domain.get_instance("i-live")).status == "starting"
    assert (await daemon_domain.get_instance("i-dead")).status == "error"
    assert (await daemon_domain.get_instance("i-legacy-live")).status == "running"
    assert (await daemon_domain.get_instance("i-legacy-stale")).status == "error"


async def test_mark_expired_instances_can_spare_its_own_record(lease_db: Path) -> None:
    now = datetime.now(UTC)
    await _seed_instance(
        instance_id="i-dead-1", lease_until=now - timedelta(seconds=1), heartbeat_at=now
    )
    await _seed_instance(
        instance_id="i-dead-2", lease_until=now - timedelta(seconds=1), heartbeat_at=now
    )

    marked = await daemon_domain.mark_expired_instances(
        reason="x", now=now, exclude_instance_id="i-dead-2"
    )

    assert marked == 1
    assert (await daemon_domain.get_instance("i-dead-1")).status == "error"
    assert (await daemon_domain.get_instance("i-dead-2")).status == "running"


async def test_fail_stale_running_spares_commands_of_live_instances(lease_db: Path) -> None:
    await domain_accounts.create_account("acc-1")
    now = datetime.now(UTC)
    await _seed_instance(
        instance_id="i-live", lease_until=now + timedelta(minutes=5), heartbeat_at=now
    )
    await _seed_instance(
        instance_id="i-dead",
        lease_until=now - timedelta(seconds=1),
        heartbeat_at=now - timedelta(minutes=5),
    )
    live_command = await worker_commands.submit("acc-1", "start", requested_by="test")
    dead_command = await worker_commands.submit("acc-1", "stop", requested_by="test")
    assert live_command is not None
    assert dead_command is not None
    assert await worker_commands.claim(live_command.command_id, daemon_instance_id="i-live") is True
    assert await worker_commands.claim(dead_command.command_id, daemon_instance_id="i-dead") is True

    failed = await worker_commands.fail_stale_running(reason="owner lease expired")

    assert failed == 1
    surviving = await worker_commands.get(live_command.command_id)
    assert surviving is not None
    assert surviving.status == WorkerCommandStatus.RUNNING.value
    expired = await worker_commands.get(dead_command.command_id)
    assert expired is not None
    assert expired.status == WorkerCommandStatus.FAILED.value
