"""Regression tests for the Phase 1 observability migration."""

from xianyu_agent.application.health import observability, soak
from xianyu_agent.cli.commands import daemon as daemon_cli
from xianyu_agent.services import observability as legacy_observability
from xianyu_agent.tui import app as tui_app, widgets


def test_legacy_observability_is_canonical_module() -> None:
    assert legacy_observability is observability


def test_primary_callers_use_canonical_observability() -> None:
    assert soak.build_runtime_snapshot is observability.build_runtime_snapshot
    assert soak.RuntimeSnapshot is observability.RuntimeSnapshot
    assert daemon_cli.build_runtime_snapshot is observability.build_runtime_snapshot
    assert tui_app.build_runtime_snapshot is observability.build_runtime_snapshot
    assert widgets.build_runtime_snapshot is observability.build_runtime_snapshot
    assert widgets.RuntimeSnapshot is observability.RuntimeSnapshot
