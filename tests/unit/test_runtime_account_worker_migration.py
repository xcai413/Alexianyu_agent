"""Regression tests for the Phase 1 AccountWorker module migration."""

from xianyu_agent.runtime.account_pool import AccountWorker as PoolAccountWorker
from xianyu_agent.runtime.account_worker import AccountWorker
from xianyu_agent.services import account_worker as legacy_account_worker


def test_legacy_account_worker_reexports_canonical_type() -> None:
    assert legacy_account_worker.AccountWorker is AccountWorker


def test_runtime_account_pool_uses_canonical_account_worker() -> None:
    assert PoolAccountWorker is AccountWorker
