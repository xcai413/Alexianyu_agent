"""Regression tests for the Phase 1 runtime lock migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from xianyu_agent.runtime.account_lock import (
    AccountConnectionAlreadyRunningError,
    AccountConnectionLock,
)
from xianyu_agent.runtime.daemon_lock import DaemonAlreadyRunningError, DaemonLock
from xianyu_agent.services import (
    account_lock as legacy_account_lock,
    daemon_lock as legacy_daemon_lock,
)


def test_legacy_lock_modules_reexport_canonical_runtime_types() -> None:
    assert legacy_daemon_lock.DaemonLock is DaemonLock
    assert legacy_daemon_lock.DaemonAlreadyRunningError is DaemonAlreadyRunningError
    assert legacy_account_lock.AccountConnectionLock is AccountConnectionLock
    assert (
        legacy_account_lock.AccountConnectionAlreadyRunningError
        is AccountConnectionAlreadyRunningError
    )


def test_runtime_daemon_lock_preserves_single_owner_behavior(tmp_path: Path) -> None:
    path = tmp_path / "daemon.lock"
    first = DaemonLock(path)
    second = DaemonLock(path)

    first.acquire(instance_id="runtime-one")
    try:
        with pytest.raises(DaemonAlreadyRunningError):
            second.acquire(instance_id="runtime-two")
    finally:
        first.release()

    second.acquire(instance_id="runtime-two")
    second.release()


def test_runtime_account_lock_preserves_single_connection_behavior(tmp_path: Path) -> None:
    path = tmp_path / "account.lock"
    first = AccountConnectionLock(path)
    second = AccountConnectionLock(path)

    first.acquire(owner_id="ws:first")
    try:
        with pytest.raises(AccountConnectionAlreadyRunningError):
            second.acquire(owner_id="ws:second")
    finally:
        first.release()

    second.acquire(owner_id="ws:second")
    second.release()
