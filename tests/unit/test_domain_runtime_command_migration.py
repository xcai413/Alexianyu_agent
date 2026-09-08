"""Regression tests for the runtime command domain responsibility migration."""

from xianyu_agent.domain import worker_commands as legacy_worker_commands
from xianyu_agent.domain.account import risk
from xianyu_agent.domain.runtime import command
from xianyu_agent.runtime import daemon


def test_legacy_worker_commands_module_aliases_canonical_module() -> None:
    assert legacy_worker_commands is command
    assert legacy_worker_commands.submit is command.submit
    assert legacy_worker_commands.claim is command.claim
    assert legacy_worker_commands.complete is command.complete


def test_runtime_daemon_uses_canonical_runtime_command_and_account_risk() -> None:
    assert daemon.worker_commands is command
    assert daemon.worker_risk is risk
