"""Daemon wiring for the bounded platform automation pass."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.runtime.daemon import RuntimeDaemon
from xianyu_agent.runtime.daemon_lock import DaemonLock


class FakeScheduler:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def run_once(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            seeded=0,
            recovered=0,
            runner=SimpleNamespace(
                claimed=0, completed=0, released=0, stale=0
            ),
        )


class FakePool:
    def bind_instance(self, _instance_id: str) -> None:
        return None

    def start_all(self) -> list[str]:
        return []

    async def renew_owned_leases(self) -> None:
        return None

    async def reconcile_enabled_accounts(self) -> dict[str, list[str]]:
        return {"started": [], "stopped": []}

    async def stop_all(self) -> None:
        return None

    async def release_all_leases(self) -> None:
        return None


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    # 覆盖仓库外的 .env;测试必须固定在默认 observe,不能被真实运行配置污染。
    monkeypatch.setenv("XIANYU_AUTOMATION_MODE", "observe")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
async def daemon_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "daemon-automation.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_daemon_runs_injected_platform_scheduler() -> None:
    scheduler = FakeScheduler()
    daemon = RuntimeDaemon(
        platform_scheduler=scheduler,
        install_signal_handlers=False,
        configure_logging=False,
    )

    await daemon._run_platform_automation()

    assert scheduler.calls == 1


@pytest.mark.asyncio
async def test_platform_scheduler_failure_never_ends_daemon_pass() -> None:
    scheduler = FakeScheduler(error=RuntimeError("boom"))
    daemon = RuntimeDaemon(
        platform_scheduler=scheduler,
        install_signal_handlers=False,
        configure_logging=False,
    )

    await daemon._run_platform_automation()

    assert scheduler.calls == 1


@pytest.mark.asyncio
async def test_daemon_loop_runs_platform_scheduler(
    daemon_db: Path,
) -> None:
    scheduler = FakeScheduler()

    async def pool_factory() -> FakePool:
        return FakePool()

    daemon = RuntimeDaemon(
        heartbeat_interval_s=0.02,
        reconcile_interval_s=1,
        soak_interval_s=1,
        automation_interval_s=0.01,
        pool_factory=pool_factory,
        platform_scheduler=scheduler,
        lock=DaemonLock(daemon_db / "runtime" / "automation.lock"),
        install_signal_handlers=False,
        configure_logging=False,
    )
    task = asyncio.create_task(daemon.run())
    for _ in range(100):
        if scheduler.calls:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("daemon did not run platform scheduler")

    daemon.request_stop()
    assert await asyncio.wait_for(task, timeout=2) is False
    assert scheduler.calls >= 1


def test_default_platform_scheduler_inherits_global_observe_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingScheduler:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    daemon_module = __import__(
        "xianyu_agent.runtime.daemon", fromlist=["PlatformAutomationScheduler"]
    )
    monkeypatch.setattr(daemon_module, "PlatformAutomationScheduler", CapturingScheduler)
    daemon = RuntimeDaemon(install_signal_handlers=False, configure_logging=False)

    scheduler = daemon._build_platform_scheduler()

    assert isinstance(scheduler, CapturingScheduler)
    assert captured["active"] is False
    assert captured["timezone"] == "Asia/Shanghai"
    assert captured["instance_id"] == daemon.instance_id
