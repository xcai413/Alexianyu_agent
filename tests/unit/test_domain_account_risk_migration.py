"""Regression tests for the account risk domain responsibility migration."""

from xianyu_agent.domain import worker_commands, worker_risk as legacy_worker_risk
from xianyu_agent.domain.account import risk
from xianyu_agent.runtime import account_worker


def test_legacy_worker_risk_module_aliases_canonical_module() -> None:
    assert legacy_worker_risk is risk
    assert legacy_worker_risk.WorkerRiskCircuit is risk.WorkerRiskCircuit
    assert legacy_worker_risk.open_user_validate is risk.open_user_validate


def test_runtime_and_worker_commands_use_canonical_account_risk() -> None:
    assert account_worker.worker_risk is risk
    assert worker_commands.worker_risk is risk
