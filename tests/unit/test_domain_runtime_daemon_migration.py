"""Regression tests for the runtime daemon domain responsibility migration."""

from xianyu_agent.application.health import observability
from xianyu_agent.domain import daemon as legacy_daemon
from xianyu_agent.domain.runtime import daemon
from xianyu_agent.runtime import daemon as runtime_daemon


def test_legacy_daemon_module_aliases_canonical_module() -> None:
    assert legacy_daemon is daemon
    assert legacy_daemon.DaemonControl is daemon.DaemonControl
    assert legacy_daemon.touch_heartbeat is daemon.touch_heartbeat
    assert legacy_daemon.request_restart is daemon.request_restart


def test_runtime_and_observability_use_canonical_daemon_domain() -> None:
    assert runtime_daemon.daemon_domain is daemon
    assert observability.daemon_domain is daemon
