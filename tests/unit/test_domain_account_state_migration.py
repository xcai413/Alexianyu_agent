"""Regression tests for the account state domain responsibility migration."""

from xianyu_agent.application.health import observability
from xianyu_agent.domain import accounts as legacy_accounts
from xianyu_agent.domain.account import state
from xianyu_agent.runtime import account_pool, daemon as runtime_daemon


def test_legacy_accounts_module_aliases_canonical_state_module() -> None:
    assert legacy_accounts is state
    assert legacy_accounts.get_account is state.get_account
    assert legacy_accounts.list_accounts is state.list_accounts
    assert legacy_accounts.worker_status_for is state.worker_status_for


def test_runtime_and_observability_use_canonical_account_state() -> None:
    assert account_pool.domain_accounts is state
    assert runtime_daemon.domain_accounts is state
    assert observability.domain_accounts is state
