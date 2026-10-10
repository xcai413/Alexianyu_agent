"""daemon 的告警轮次:默认关闭,失败不影响主循环。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.runtime.daemon import RuntimeDaemon


class FakeNotifier:
    def __init__(self, *, enabled: bool = True, error: Exception | None = None) -> None:
        self.enabled = enabled
        self.error = error
        self.calls = 0

    async def dispatch(self, items):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(sent=0, skipped=0, failed=0, disabled=False)


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("XIANYU_ALERT_WEBHOOK_URL", raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
async def alert_daemon_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """告警轮次会读一次待办清单,所以需要可用的库结构。"""
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "alert-daemon.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield tmp_path
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_disabled_notifier_is_a_noop() -> None:
    notifier = FakeNotifier(enabled=False)
    daemon = RuntimeDaemon(
        alert_notifier=notifier, install_signal_handlers=False, configure_logging=False
    )

    await daemon._dispatch_alerts()

    assert notifier.calls == 0


@pytest.mark.asyncio
async def test_notifier_failure_never_ends_the_daemon_pass(
    alert_daemon_db: Path,
) -> None:
    notifier = FakeNotifier(error=RuntimeError("boom"))
    daemon = RuntimeDaemon(
        alert_notifier=notifier, install_signal_handlers=False, configure_logging=False
    )

    await daemon._dispatch_alerts()

    assert notifier.calls == 1


@pytest.mark.asyncio
async def test_configured_channels_take_priority_over_the_env_webhook(
    alert_daemon_db: Path,
) -> None:
    """库里配了渠道就不再走环境变量那条路,避免同一事项被两条路径各发一次。"""

    class FakeDispatcher:
        async def dispatch(self, items):
            return SimpleNamespace(sent=0, skipped=0, failed=0, disabled=False)

    notifier = FakeNotifier()
    daemon = RuntimeDaemon(
        alert_notifier=notifier, install_signal_handlers=False, configure_logging=False
    )
    daemon._alert_dispatcher = FakeDispatcher()

    await daemon._dispatch_alerts()

    assert notifier.calls == 0


@pytest.mark.asyncio
async def test_env_webhook_is_the_fallback_when_no_channel_is_configured(
    alert_daemon_db: Path,
) -> None:
    notifier = FakeNotifier()
    daemon = RuntimeDaemon(
        alert_notifier=notifier, install_signal_handlers=False, configure_logging=False
    )

    await daemon._dispatch_alerts()

    assert notifier.calls == 1


def test_default_notifier_follows_the_environment_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = RuntimeDaemon(install_signal_handlers=False, configure_logging=False)

    assert daemon._build_alert_notifier().enabled is False

    monkeypatch.setenv("XIANYU_ALERT_WEBHOOK_URL", "https://alerts.example.com/hook")
    reset_settings_cache()

    assert daemon._build_alert_notifier().enabled is True
