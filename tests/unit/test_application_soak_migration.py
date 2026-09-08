"""Regression tests for the Phase 1 soak-monitor migration."""

from xianyu_agent.application.health import soak
from xianyu_agent.cli.commands import soak as soak_cli
from xianyu_agent.runtime import watchdog
from xianyu_agent.services import soak_monitor as legacy_soak_monitor


def test_legacy_soak_monitor_is_canonical_module() -> None:
    assert legacy_soak_monitor is soak


def test_runtime_and_cli_use_canonical_soak_service() -> None:
    assert watchdog.sample_active_soaks is soak.sample_active_soaks
    assert soak_cli.start_soak is soak.start_soak
    assert soak_cli.latest_soak is soak.latest_soak
    assert soak_cli.stop_soak is soak.stop_soak
    assert soak_cli.evidence_path is soak.evidence_path
