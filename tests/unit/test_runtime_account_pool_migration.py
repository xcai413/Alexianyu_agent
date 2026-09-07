"""Regression tests for the Phase 1 AccountPool module migration."""

from xianyu_agent.runtime import daemon as runtime_daemon
from xianyu_agent.runtime.account_pool import AccountPool
from xianyu_agent.services import account_pool as legacy_account_pool


def test_legacy_account_pool_reexports_canonical_type() -> None:
    assert legacy_account_pool.AccountPool is AccountPool


def test_runtime_daemon_uses_canonical_account_pool() -> None:
    assert runtime_daemon.AccountPool is AccountPool
