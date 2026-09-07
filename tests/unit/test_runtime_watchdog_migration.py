"""Regression tests for the Phase 1 watchdog migration."""

from xianyu_agent.runtime import watchdog as runtime_watchdog
from xianyu_agent.services import watchdog_runner as legacy_watchdog


def test_legacy_watchdog_reexports_canonical_entrypoints() -> None:
    assert legacy_watchdog.check_and_recover is runtime_watchdog.check_and_recover
    assert legacy_watchdog._run_once is runtime_watchdog._run_once
    assert legacy_watchdog.main is runtime_watchdog.main


def test_legacy_watchdog_reexports_runtime_dependencies() -> None:
    assert legacy_watchdog.daemon_domain is runtime_watchdog.daemon_domain
    assert legacy_watchdog.observe_daemon is runtime_watchdog.observe_daemon
    assert legacy_watchdog.sample_active_soaks is runtime_watchdog.sample_active_soaks
    assert legacy_watchdog.is_service_paused is runtime_watchdog.is_service_paused
    assert legacy_watchdog.end_task is runtime_watchdog.end_task
    assert legacy_watchdog.start_task is runtime_watchdog.start_task
