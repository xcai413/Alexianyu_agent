"""Regressions for AccountPool restart startup ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from xianyu_agent.application.session.health import (
    CredentialHealth,
    CredentialHealthState,
    CredentialResult,
    CredentialResultState,
)
from xianyu_agent.application.session.ports import CredentialFailureCode
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain.account import state as domain_accounts
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.protocol.events import ConnectionState, ConnectionStateChanged
from xianyu_agent.runtime import account_pool as account_pool_mod
from xianyu_agent.runtime.account_pool import AccountPool
from xianyu_agent.runtime.account_worker import AccountWorker
from xianyu_agent.runtime.recovery import RecoverySupervisor


class FakeClient:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.state = ConnectionState.IDLE
        self.subscription_ready = None
        self.on_state: Callable[[ConnectionStateChanged], Awaitable[None]] | None = None
        self.on_auth_failure = None
        self.on_event = None
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        self.state = ConnectionState.CONNECTING

    async def stop(self) -> None:
        self.stop_calls += 1
        self.state = ConnectionState.DISCONNECTED

    async def send_text(self, _text: str) -> bool:
        return False


class PermanentlyRetryingCredentials:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.ensure_calls = 0

    async def ensure(self, account_id: str) -> CredentialResult[str]:
        assert account_id == self.account_id
        self.ensure_calls += 1
        return CredentialResult(
            state=CredentialResultState.RETRYABLE_FAILURE,
            health=CredentialHealth(account_id=account_id, state=CredentialHealthState.HEALTHY),
            code=CredentialFailureCode.NETWORK_ERROR.value,
        )

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[str]:
        raise AssertionError((account_id, validation_recovery))


class TerminalCredentials:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.ensure_calls = 0

    async def ensure(self, account_id: str) -> CredentialResult[str]:
        assert account_id == self.account_id
        self.ensure_calls += 1
        return CredentialResult(
            state=CredentialResultState.TERMINAL_FAILURE,
            health=CredentialHealth(account_id=account_id, state=CredentialHealthState.UNUSABLE),
            code=CredentialFailureCode.IDENTITY_MISSING.value,
        )

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[str]:
        raise AssertionError((account_id, validation_recovery))


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    previous_engine = db_mod.async_engine
    previous_factory = db_mod.async_session_factory
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "pool-restart-ownership.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    test_engine = db_mod.async_engine
    try:
        await db_mod.init_db()
        yield
    finally:
        await test_engine.dispose()
        db_mod.async_engine = previous_engine
        db_mod.async_session_factory = previous_factory
        reset_settings_cache()


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(5000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise TimeoutError("condition did not become true")


@pytest.mark.asyncio
async def test_restart_returns_without_waiting_for_retrying_startup(clean_db) -> None:
    account_id = "restart-retrying"
    await domain_accounts.create_account(account_id, enabled=True)
    retry_entered = asyncio.Event()
    never_release = asyncio.Event()

    async def retry_sleep(_delay: float) -> None:
        retry_entered.set()
        await never_release.wait()

    credentials = PermanentlyRetryingCredentials(account_id)
    client = FakeClient(account_id)
    worker = AccountWorker(
        account_id,
        client=cast(Any, client),
        credential_supervisor=cast(Any, credentials),
        recovery_supervisor=RecoverySupervisor(cast(Any, credentials)),
        retry_sleep=retry_sleep,
        persist_events=False,
        automation_mode="passive",
    )
    pool = AccountPool(worker_factory=lambda _account_id: worker)

    result = await asyncio.wait_for(pool.restart_worker(account_id), timeout=0.1)

    assert result == "restarted"
    await retry_entered.wait()
    startup = worker._startup_task
    assert startup is not None
    assert startup.done() is False
    assert pool._startup_tasks.get(account_id) is startup
    assert pool.get(account_id) is worker
    assert client.start_calls == 0

    await pool.stop_all()
    await asyncio.sleep(0)
    assert worker._startup_task is None
    assert worker.is_running is False


@pytest.mark.asyncio
async def test_restart_terminal_startup_is_retired_and_reconcile_can_replace(
    clean_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "restart-terminal"
    await domain_accounts.create_account(account_id, enabled=True)
    credentials = TerminalCredentials(account_id)
    terminal_client = FakeClient(account_id)
    terminal_worker = AccountWorker(
        account_id,
        client=cast(Any, terminal_client),
        credential_supervisor=cast(Any, credentials),
        recovery_supervisor=RecoverySupervisor(cast(Any, credentials)),
        persist_events=False,
        automation_mode="passive",
    )
    pool = AccountPool(worker_factory=lambda _account_id: terminal_worker)

    result = await pool.restart_worker(account_id)

    assert result == "restarted"
    await wait_until(lambda: terminal_worker.state is WorkerState.ERROR)
    await wait_until(lambda: not pool.has(account_id))
    assert terminal_client.start_calls == 0
    assert account_id not in pool._startup_tasks

    async def desired_accounts():
        return [SimpleNamespace(account_id=account_id)]

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )
    replacement_client = FakeClient(account_id)

    def build_replacement(replacement_id: str) -> AccountWorker:
        assert replacement_id == account_id
        return AccountWorker(
            replacement_id,
            client=cast(Any, replacement_client),
            persist_events=False,
            automation_mode="passive",
        )

    pool._worker_factory = build_replacement
    reconcile = await pool.reconcile_desired_accounts()

    assert reconcile["started"] == [account_id]
    await wait_until(lambda: replacement_client.start_calls == 1)
    assert pool.has(account_id) is True
    await pool.stop_all()
