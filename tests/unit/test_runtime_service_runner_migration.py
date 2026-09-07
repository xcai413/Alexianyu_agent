"""Regression tests for the Phase 1 service-runner migration."""

from xianyu_agent.runtime import service_runner as runtime_service_runner
from xianyu_agent.runtime.daemon import run_runtime_daemon
from xianyu_agent.services import service_runner as legacy_service_runner


def test_legacy_service_runner_reexports_canonical_entrypoint() -> None:
    assert legacy_service_runner.main is runtime_service_runner.main
    assert legacy_service_runner.run_runtime_daemon is runtime_service_runner.run_runtime_daemon
    assert legacy_service_runner.is_service_paused is runtime_service_runner.is_service_paused


def test_runtime_service_runner_uses_canonical_daemon_entrypoint() -> None:
    assert runtime_service_runner.run_runtime_daemon is run_runtime_daemon
