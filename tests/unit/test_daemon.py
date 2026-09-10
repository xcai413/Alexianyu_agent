"""P0.1 RuntimeDaemon、单实例锁和日志脱敏测试。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, daemon as daemon_domain
from xianyu_agent.services.account_pool import AccountPool
from xianyu_agent.services.daemon_lock import DaemonAlreadyRunningError, DaemonLock
from xianyu_agent.services.logging_setup import configure_daemon_logging, redact_text
from xianyu_agent.services.runtime_daemon import RuntimeDaemon
from xianyu_agent.utils.process_utils import pid_alive


class FakeWorker:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class FakePool:
    def __init__(self) -> None:
        self.workers = {"a": FakeWorker("a"), "b": FakeWorker("b")}
        self.reconciled = 0
        self.stopped = 0

    def start_all(self) -> list[str]:
        for worker in self.workers.values():
            worker.start()
        return list(self.workers)

    async def reconcile_enabled_accounts(self) -> dict[str, list[str]]:
        self.reconciled += 1
        return {"started": [], "stopped": []}

    async def stop_all(self) -> None:
        self.stopped += 1
        for worker in self.workers.values():
            await worker.stop()


@pytest.fixture
async def daemon_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "daemon.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_runtime_daemon_lifecycle_and_persisted_stop(daemon_db: Path) -> None:
    pool = FakePool()

    async def pool_factory():
        return pool

    runtime = RuntimeDaemon(
        heartbeat_interval_s=0.02,
        reconcile_interval_s=0.01,
        pool_factory=pool_factory,
        lock=DaemonLock(daemon_db / "runtime" / "daemon.lock"),
        install_signal_handlers=False,
        configure_logging=False,
    )
    task = asyncio.create_task(runtime.run())
    for _ in range(100):
        row = await daemon_domain.latest_instance()
        if row is not None and row.status == "running":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("daemon did not become running")

    assert pool.workers["a"].started == 1
    assert pool.workers["b"].started == 1
    for _ in range(100):
        if pool.reconciled >= 1:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("daemon did not reconcile enabled accounts")
    assert await daemon_domain.request_shutdown(runtime.instance_id) == 1
    assert await asyncio.wait_for(task, timeout=2.0) is False

    row = await daemon_domain.get_instance(runtime.instance_id)
    assert row is not None
    assert row.status == "stopped"
    assert row.stopped_at is not None
    assert pool.stopped == 1
    assert pool.reconciled >= 1
    assert (daemon_db / "runtime" / "daemon.lock").exists()


@pytest.mark.asyncio
async def test_runtime_daemon_returns_restart_request(daemon_db: Path) -> None:
    pool = FakePool()

    async def pool_factory():
        return pool

    runtime = RuntimeDaemon(
        heartbeat_interval_s=0.02,
        reconcile_interval_s=0.02,
        pool_factory=pool_factory,
        lock=DaemonLock(daemon_db / "runtime" / "restart.lock"),
        install_signal_handlers=False,
        configure_logging=False,
    )
    task = asyncio.create_task(runtime.run())
    for _ in range(100):
        row = await daemon_domain.latest_instance()
        if row is not None and row.status == "running":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("daemon did not become running")

    assert await daemon_domain.request_restart(runtime.instance_id) == 1
    assert await asyncio.wait_for(task, timeout=2.0) is True
    row = await daemon_domain.get_instance(runtime.instance_id)
    assert row is not None
    assert row.status == "stopped"
    assert row.restart_requested is True
    assert pool.stopped == 1


@pytest.mark.asyncio
async def test_runtime_daemon_reconciles_account_changes(daemon_db: Path) -> None:
    workers: dict[str, FakeWorker] = {}

    def factory(account_id: str):
        worker = FakeWorker(account_id)
        workers[account_id] = worker
        return worker

    await domain_accounts.create_account("a", enabled=True)
    await domain_accounts.set_desired_state("a", "running")
    pool = AccountPool(worker_factory=factory)
    await pool.reconcile_enabled_accounts()
    assert pool.account_ids == ["a"]
    assert workers["a"].started == 1

    await domain_accounts.set_enabled("a", False)
    await domain_accounts.create_account("b", enabled=True)
    await domain_accounts.set_desired_state("b", "running")
    changes = await pool.reconcile_enabled_accounts()

    assert changes == {"started": ["b"], "stopped": ["a"]}
    assert workers["b"].started == 1
    assert pool.account_ids == ["b"]

    retirement = pool._retirement_tasks["a"]
    await retirement

    assert workers["a"].stopped == 1


def test_daemon_lock_rejects_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    first = DaemonLock(path)
    second = DaemonLock(path)
    first.acquire(instance_id="one")
    try:
        with pytest.raises(DaemonAlreadyRunningError):
            second.acquire(instance_id="two")
    finally:
        first.release()
    second.acquire(instance_id="two")
    second.release()


def test_daemon_lock_propagates_metadata_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "daemon.lock"
    lock = DaemonLock(path)

    class BrokenHandle:
        def seek(self, *_args):
            return 0

        def read(self, *_args):
            return "x"

        def truncate(self):
            return 0

        def write(self, _value):
            raise OSError(5, "metadata write failed")

        def flush(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: BrokenHandle())
    monkeypatch.setattr(lock, "_lock", lambda _handle: None)
    monkeypatch.setattr(lock, "_unlock", lambda _handle: None)
    with pytest.raises(OSError, match="metadata write failed"):
        lock.acquire(instance_id="broken")


def test_pid_alive_handles_current_and_invalid_pid() -> None:
    assert pid_alive(os.getpid()) is True
    assert pid_alive(-1) is False
    assert pid_alive(2_147_483_647) is False


def test_redact_text_hides_secrets() -> None:
    source = (
        "Cookie: unb=123; _m_h5_tk=secret; cookie2=abc "
        "token=token-secret FERNET_KEY=fernet-secret code=CARD-SECRET "
        "content=buyer-secret"
    )
    redacted = redact_text(source)
    for secret in (
        "123",
        "secret",
        "abc",
        "token-secret",
        "fernet-secret",
        "CARD-SECRET",
        "buyer-secret",
    ):
        assert secret not in redacted
    assert "<redacted>" in redacted
    no_prefix = redact_text(
        "unb=9988776655; cookie2=session-secret token=token-secret content=buyer text here"
    )
    assert "9988776655" not in no_prefix
    assert "session-secret" not in no_prefix
    assert "token-secret" not in no_prefix
    assert "buyer text here" not in no_prefix


def test_configure_logging_writes_redacted_rotating_file(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "daemon.log"
    configure_daemon_logging(path, level="INFO")
    root = logging.getLogger()
    try:
        logger = logging.getLogger("xianyu_agent.test.daemon")
        logger.info("Cookie: unb=123; code=CARD-SECRET content=buyer-secret")
        for handler in root.handlers:
            if getattr(handler, "_xianyu_daemon_handler", False):
                handler.flush()
        content = path.read_text(encoding="utf-8")
        assert "unb=123" not in content
        assert "CARD-SECRET" not in content
        assert "buyer-secret" not in content
        assert "Cookie: <redacted>" in content
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        for handler in list(root.handlers):
            if getattr(handler, "_xianyu_daemon_handler", False):
                root.removeHandler(handler)
                handler.close()


@pytest.mark.asyncio
async def test_restart_request_sets_control_flags(daemon_db: Path) -> None:
    row = await daemon_domain.create_instance(instance_id="restart-me", pid=123, version="test")
    await daemon_domain.mark_running(row.instance_id)
    assert await daemon_domain.request_restart(row.instance_id) == 1
    control = await daemon_domain.touch_heartbeat(row.instance_id)
    assert control.shutdown_requested is True
    assert control.restart_requested is True
