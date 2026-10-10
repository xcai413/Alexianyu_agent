"""Daemon wiring tests for the periodic credential renewal pass."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.runtime.daemon import RuntimeDaemon


class FakeScheduler:
    def __init__(self, *, reports: list[object], error: Exception | None = None) -> None:
        self.reports = reports
        self.error = error
        self.calls = 0

    async def run_once(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return tuple(self.reports)


def _daemon(scheduler: FakeScheduler | None, **kwargs: object) -> RuntimeDaemon:
    return RuntimeDaemon(
        pool_factory=None,
        install_signal_handlers=False,
        configure_logging=False,
        renewal_scheduler=scheduler,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.mark.asyncio
async def test_renewal_pass_runs_the_injected_scheduler() -> None:
    scheduler = FakeScheduler(
        reports=[
            SimpleNamespace(account_id="acc-1", success=True, need_human_login=False),
            SimpleNamespace(account_id="acc-2", success=False, need_human_login=True),
        ]
    )
    daemon = _daemon(scheduler)

    await daemon._renew_credentials()

    assert scheduler.calls == 1


@pytest.mark.asyncio
async def test_renewal_failure_never_ends_the_daemon_loop() -> None:
    scheduler = FakeScheduler(reports=[], error=RuntimeError("boom"))
    daemon = _daemon(scheduler)

    await daemon._renew_credentials()

    assert scheduler.calls == 1


@pytest.mark.asyncio
async def test_renewal_respects_the_disable_switch() -> None:
    os.environ["XIANYU_CREDENTIAL_RENEWAL_ENABLED"] = "false"
    reset_settings_cache()
    scheduler = FakeScheduler(reports=[])
    daemon = _daemon(scheduler)

    assert daemon.renewal_enabled is False
    os.environ.pop("XIANYU_CREDENTIAL_RENEWAL_ENABLED", None)
    reset_settings_cache()
