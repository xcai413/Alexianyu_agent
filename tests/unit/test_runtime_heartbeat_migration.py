"""Regression tests for the Phase 1 heartbeat maintenance migration."""

from xianyu_agent.cli.commands import maintenance
from xianyu_agent.runtime.heartbeat import purge_old_messages
from xianyu_agent.services import heartbeat as legacy_heartbeat


def test_legacy_heartbeat_reexports_canonical_function() -> None:
    assert legacy_heartbeat.purge_old_messages is purge_old_messages


def test_maintenance_cli_uses_canonical_heartbeat_function() -> None:
    assert maintenance.purge_old_messages is purge_old_messages
