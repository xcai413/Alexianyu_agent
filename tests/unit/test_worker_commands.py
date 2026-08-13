"""P0.2 worker_commands 与账号级 daemon 控制测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.cli.main import app as cli_app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, WorkerCommand, database as db_mod, get_async_session
from xianyu_agent.domain import (
    accounts as domain_accounts,
    daemon as daemon_domain,
    worker_commands,
)
from xianyu_agent.services.daemon_lock import DaemonLock
from xianyu_agent.services.runtime_daemon import RuntimeDaemon


class CommandPool:
    def __init__(self) -> None:
        self.running: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.stopped_all = 0

    def start_all(self) -> list[str]:
        return []

    async def reconcile_enabled_accounts(self) -> dict[str, list[str]]:
        return {"started": [], "stopped": []}

    def ensure_started(self, account_id: str) -> str:
        self.calls.append(("start", account_id))
        if account_id in self.running:
            return "already_running"
        self.running.add(account_id)
        return "started"

    async def ensure_stopped(self, account_id: str) -> str:
        self.calls.append(("stop", account_id))
        if account_id not in self.running:
            return "already_stopped"
        self.running.remove(account_id)
        return "stopped"

    async def restart_worker(self, account_id: str) -> str:
        self.calls.append(("restart", account_id))
        self.running.add(account_id)
        return "restarted"

    async def stop_all(self) -> None:
        self.stopped_all += 1
        self.running.clear()


@pytest.fixture
async def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "control.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_submit_updates_desired_state_and_validates_account(control_db: Path) -> None:
    _ = control_db
    await domain_accounts.create_account("a", enabled=True)
    account = await domain_accounts.get_account("a")
    assert account is not None
    assert account.desired_state == "stopped"

    start = await worker_commands.submit("a", "start")
    assert start is not None
    assert start.status == "pending"
    account = await domain_accounts.get_account("a")
    assert account is not None
    assert account.desired_state == "running"

    stop = await worker_commands.submit("a", "stop")
    assert stop is not None
    account = await domain_accounts.get_account("a")
    assert account is not None
    assert account.desired_state == "stopped"

    assert await worker_commands.submit("missing", "start") is None
    await domain_accounts.set_enabled("a", False)
    with pytest.raises(ValueError, match="已禁用"):
        await worker_commands.submit("a", "start")
    with pytest.raises(ValueError, match="invalid worker action"):
        await worker_commands.submit("a", "explode")


@pytest.mark.asyncio
async def test_claim_is_atomic_and_complete_is_terminal(control_db: Path) -> None:
    _ = control_db
    await domain_accounts.create_account("a", enabled=True)
    command = await worker_commands.submit("a", "start")
    assert command is not None
    claimed = await asyncio.gather(
        worker_commands.claim(command.command_id, daemon_instance_id="d1"),
        worker_commands.claim(command.command_id, daemon_instance_id="d2"),
    )
    assert sorted(claimed) == [False, True]
    await worker_commands.complete(command.command_id, success=True, result="started")
    row = await worker_commands.get(command.command_id)
    assert row is not None
    assert row.status == "succeeded"
    assert row.result == "started"
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_submit_many_is_atomic_for_enabled_accounts(control_db: Path) -> None:
    _ = control_db
    await domain_accounts.create_account("a", enabled=True)
    await domain_accounts.create_account("b", enabled=True)
    await domain_accounts.create_account("disabled", enabled=False)

    rows = await worker_commands.submit_many("start")
    assert len(rows) == 2
    assert {await worker_commands.account_key(row) for row in rows} == {"a", "b"}
    assert all(row.status == "pending" for row in rows)
    a = await domain_accounts.get_account("a")
    b = await domain_accounts.get_account("b")
    disabled = await domain_accounts.get_account("disabled")
    assert a is not None
    assert b is not None
    assert disabled is not None
    assert a.desired_state == "running"
    assert b.desired_state == "running"
    assert disabled.desired_state == "stopped"


@pytest.mark.asyncio
async def test_disabling_account_forces_desired_stopped(control_db: Path) -> None:
    _ = control_db
    await domain_accounts.create_account("a", enabled=True)
    await domain_accounts.set_desired_state("a", "running")
    assert await domain_accounts.set_enabled("a", False) is True
    account = await domain_accounts.get_account("a")
    assert account is not None
    assert account.enabled is False
    assert account.desired_state == "stopped"


@pytest.mark.asyncio
async def test_daemon_executes_start_restart_stop_commands(control_db: Path) -> None:
    pool = CommandPool()

    async def pool_factory():
        return pool

    runtime = RuntimeDaemon(
        heartbeat_interval_s=0.02,
        reconcile_interval_s=0.05,
        pool_factory=pool_factory,
        lock=DaemonLock(control_db / "runtime" / "daemon.lock"),
        install_signal_handlers=False,
        configure_logging=False,
    )
    await domain_accounts.create_account("a", enabled=True)
    task = asyncio.create_task(runtime.run())
    await _wait_daemon_running()

    rows = []
    for action in ("start", "restart", "stop"):
        row = await worker_commands.submit("a", action)
        assert row is not None
        rows.append(row)
        await _wait_command(row.command_id, expected="succeeded")

    assert pool.calls == [("start", "a"), ("restart", "a"), ("stop", "a")]
    assert await daemon_domain.request_shutdown(runtime.instance_id) == 1
    assert await asyncio.wait_for(task, timeout=2.0) is False
    assert pool.stopped_all == 1
    for row in rows:
        completed = await worker_commands.get(row.command_id)
        assert completed is not None
        assert completed.daemon_instance_id == runtime.instance_id


@pytest.mark.asyncio
async def test_daemon_fails_invalid_pending_command(control_db: Path) -> None:
    pool = CommandPool()

    async def pool_factory():
        return pool

    await domain_accounts.create_account("a", enabled=True)
    async with get_async_session() as session:
        account_id = (
            await session.execute(select(Account.id).where(Account.account_id == "a"))
        ).scalar_one()
        bad = WorkerCommand(
            command_id="bad-command",
            account_id=account_id,
            action="explode",
            status="pending",
            requested_by="test",
        )
        session.add(bad)
        await session.commit()

    runtime = RuntimeDaemon(
        heartbeat_interval_s=0.02,
        reconcile_interval_s=0.05,
        pool_factory=pool_factory,
        lock=DaemonLock(control_db / "runtime" / "daemon.lock"),
        install_signal_handlers=False,
        configure_logging=False,
    )
    task = asyncio.create_task(runtime.run())
    await _wait_daemon_running()
    failed = await _wait_command("bad-command", expected="failed")
    assert "unsupported worker action" in (failed.error or "")
    await daemon_domain.request_shutdown(runtime.instance_id)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_stale_running_command_is_failed_on_daemon_start(control_db: Path) -> None:
    pool = CommandPool()

    async def pool_factory():
        return pool

    await domain_accounts.create_account("a", enabled=True)
    command = await worker_commands.submit("a", "start")
    assert command is not None
    assert await worker_commands.claim(command.command_id, daemon_instance_id="dead") is True

    runtime = RuntimeDaemon(
        heartbeat_interval_s=0.02,
        reconcile_interval_s=0.05,
        pool_factory=pool_factory,
        lock=DaemonLock(control_db / "runtime" / "daemon.lock"),
        install_signal_handlers=False,
        configure_logging=False,
    )
    task = asyncio.create_task(runtime.run())
    await _wait_daemon_running()
    row = await worker_commands.get(command.command_id)
    assert row is not None
    assert row.status == "failed"
    assert "previous daemon exited" in (row.error or "")
    await daemon_domain.request_shutdown(runtime.instance_id)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_daemon_rechecks_disabled_account_before_start(control_db: Path) -> None:
    pool = CommandPool()

    async def pool_factory():
        return pool

    await domain_accounts.create_account("a", enabled=True)
    command = await worker_commands.submit("a", "start")
    assert command is not None
    await domain_accounts.set_enabled("a", False)

    runtime = RuntimeDaemon(
        heartbeat_interval_s=0.02,
        reconcile_interval_s=0.05,
        pool_factory=pool_factory,
        lock=DaemonLock(control_db / "runtime" / "daemon.lock"),
        install_signal_handlers=False,
        configure_logging=False,
    )
    task = asyncio.create_task(runtime.run())
    await _wait_daemon_running()
    failed = await _wait_command(command.command_id, expected="failed")
    assert "account disabled before command execution" in (failed.error or "")
    assert pool.calls == []
    await daemon_domain.request_shutdown(runtime.instance_id)
    await asyncio.wait_for(task, timeout=2.0)


def test_pool_cli_daemon_offline_returns_nonzero(control_db: Path) -> None:
    _ = control_db
    asyncio.run(domain_accounts.create_account("a", enabled=True))
    runner = CliRunner()
    result = runner.invoke(cli_app, ["pool", "start", "--account", "a", "--wait", "0.1"])
    assert result.exit_code == 2
    assert "daemon 不在线" in result.output
    command = asyncio.run(worker_commands.pending())
    assert len(command) == 1


def test_pool_cli_wait_zero_reports_submitted(control_db: Path) -> None:
    _ = control_db
    asyncio.run(domain_accounts.create_account("a", enabled=True))
    runner = CliRunner()
    result = runner.invoke(cli_app, ["pool", "start", "--account", "a", "--wait", "0"])
    assert result.exit_code == 0
    assert "未等待执行" in result.output


def test_pool_cli_missing_and_disabled_accounts_fail(control_db: Path) -> None:
    _ = control_db
    asyncio.run(domain_accounts.create_account("disabled", enabled=False))
    runner = CliRunner()
    missing = runner.invoke(
        cli_app, ["pool", "start", "--account", "missing", "--wait", "0"]
    )
    assert missing.exit_code == 1
    assert "不存在" in missing.output
    disabled = runner.invoke(
        cli_app, ["pool", "start", "--account", "disabled", "--wait", "0"]
    )
    assert disabled.exit_code == 1
    assert "已禁用" in disabled.output


async def _wait_daemon_running() -> None:
    for _ in range(100):
        row = await daemon_domain.latest_instance()
        if row is not None and row.status == "running":
            return
        await asyncio.sleep(0.01)
    pytest.fail("daemon did not become running")


async def _wait_command(command_id: str, *, expected: str) -> WorkerCommand:
    for _ in range(150):
        row = await worker_commands.get(command_id)
        if row is not None and row.status == expected:
            return row
        await asyncio.sleep(0.01)
    pytest.fail(f"command {command_id} did not become {expected}")
