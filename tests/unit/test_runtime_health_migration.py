"""Regression tests for the Phase 1 runtime-health migration."""

from xianyu_agent.application.health import runtime_health
from xianyu_agent.runtime import watchdog
from xianyu_agent.services import daemon_health as legacy_daemon_health
from xianyu_agent.services import observability


def test_legacy_daemon_health_is_canonical_module() -> None:
    assert legacy_daemon_health is runtime_health


def test_runtime_callers_use_canonical_health_contract() -> None:
    assert watchdog.observe_daemon is runtime_health.observe_daemon
    assert observability.observe_daemon is runtime_health.observe_daemon
    assert observability.DaemonHealth is runtime_health.DaemonHealth
