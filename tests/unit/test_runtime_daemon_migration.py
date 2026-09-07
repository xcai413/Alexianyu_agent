"""Regression tests for the Phase 1 RuntimeDaemon module migration."""

from xianyu_agent.runtime.daemon import RuntimeDaemon, run_runtime_daemon
from xianyu_agent.services import runtime_daemon as legacy_runtime_daemon, service_runner


def test_legacy_runtime_daemon_reexports_canonical_types() -> None:
    assert legacy_runtime_daemon.RuntimeDaemon is RuntimeDaemon
    assert legacy_runtime_daemon.run_runtime_daemon is run_runtime_daemon


def test_service_runner_uses_canonical_runtime_daemon_entrypoint() -> None:
    assert service_runner.run_runtime_daemon is run_runtime_daemon
