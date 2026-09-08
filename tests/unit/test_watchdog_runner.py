"""Windows daemon watchdog 单次检查测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from xianyu_agent.runtime import watchdog as watchdog_runner
from xianyu_agent.services.daemon_health import DaemonHealth


@pytest.mark.asyncio
async def test_watchdog_skips_when_service_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    started = False

    def start() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(watchdog_runner, "is_service_paused", lambda: True)
    monkeypatch.setattr(watchdog_runner, "start_task", start)
    assert await watchdog_runner.check_and_recover() is False
    assert started is False


@pytest.mark.asyncio
async def test_watchdog_keeps_healthy_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    async def latest():
        return SimpleNamespace(status="running", pid=123)

    monkeypatch.setattr(watchdog_runner, "is_service_paused", lambda: False)
    monkeypatch.setattr(watchdog_runner.daemon_domain, "latest_instance", latest)
    monkeypatch.setattr(
        watchdog_runner,
        "observe_daemon",
        lambda _row: DaemonHealth("online", True, True, 5.0, True),
    )
    monkeypatch.setattr(
        watchdog_runner,
        "start_task",
        lambda: pytest.fail("healthy daemon must not be started"),
    )
    assert await watchdog_runner.check_and_recover() is False


@pytest.mark.asyncio
async def test_watchdog_starts_dead_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    started = False

    async def latest():
        return SimpleNamespace(status="running", pid=123)

    def start() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(watchdog_runner, "is_service_paused", lambda: False)
    monkeypatch.setattr(watchdog_runner.daemon_domain, "latest_instance", latest)
    monkeypatch.setattr(
        watchdog_runner,
        "observe_daemon",
        lambda _row: DaemonHealth("dead", True, False, 5.0, False),
    )
    monkeypatch.setattr(watchdog_runner, "start_task", start)
    assert await watchdog_runner.check_and_recover() is True
    assert started is True


@pytest.mark.asyncio
async def test_watchdog_allows_fresh_starting_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    async def latest():
        return SimpleNamespace(status="starting", pid=123)

    monkeypatch.setattr(watchdog_runner, "is_service_paused", lambda: False)
    monkeypatch.setattr(watchdog_runner.daemon_domain, "latest_instance", latest)
    monkeypatch.setattr(
        watchdog_runner,
        "observe_daemon",
        lambda _row: DaemonHealth("starting", True, True, 1.0, False),
    )
    monkeypatch.setattr(
        watchdog_runner,
        "end_task",
        lambda: pytest.fail("fresh starting daemon must not be stopped"),
    )
    assert await watchdog_runner.check_and_recover() is False


@pytest.mark.asyncio
async def test_watchdog_run_once_samples_active_soaks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def recover() -> bool:
        calls.append("recover")
        return False

    async def sample() -> int:
        calls.append("sample")
        return 1

    monkeypatch.setattr(watchdog_runner, "check_and_recover", recover)
    monkeypatch.setattr(watchdog_runner, "sample_active_soaks", sample)
    await watchdog_runner._run_once()
    assert calls == ["recover", "sample"]
