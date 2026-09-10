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


@pytest.mark.asyncio
async def test_reconcile_stop_tolerates_worker_already_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AccountPool()
    first_stop_entered = asyncio.Event()
    release_first_stop = asyncio.Event()

    class ControlledWorker:
        def __init__(self, *, block: bool = False) -> None:
            self.block = block
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1
            if self.block:
                first_stop_entered.set()
                await release_first_stop.wait()

    first = ControlledWorker(block=True)
    second = ControlledWorker()
    pool._workers = cast(
        dict[str, AccountWorker],
        {"a-worker": cast(Any, first), "b-worker": cast(Any, second)},
    )

    async def desired_accounts():
        return []

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )

    reconcile_task = asyncio.create_task(pool.reconcile_desired_accounts())
    await first_stop_entered.wait()
    assert pool._workers.pop("b-worker", None) is second
    release_first_stop.set()

    result = await reconcile_task

    assert result == {"started": [], "stopped": ["a-worker"]}
    assert first.stop_calls == 1
    assert second.stop_calls == 0
    assert pool.account_ids == []


@pytest.mark.asyncio
async def test_reconcile_stop_does_not_remove_replacement_inserted_during_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AccountPool()
    old_stop_entered = asyncio.Event()
    release_old_stop = asyncio.Event()

    class OldWorker:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1
            old_stop_entered.set()
            await release_old_stop.wait()

    class ReplacementWorker:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1

    old_worker = OldWorker()
    replacement = ReplacementWorker()
    pool._workers = cast(
        dict[str, AccountWorker],
        {"race-worker": cast(Any, old_worker)},
    )

    async def desired_accounts():
        return []

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )

    reconcile_task = asyncio.create_task(pool.reconcile_desired_accounts())
    await old_stop_entered.wait()
    pool._workers["race-worker"] = cast(Any, replacement)
    release_old_stop.set()

    result = await reconcile_task

    assert result == {"started": [], "stopped": ["race-worker"]}
    assert old_worker.stop_calls == 1
    assert replacement.stop_calls == 0
    assert pool.get("race-worker") is replacement


@pytest.mark.asyncio
async def test_terminal_retirement_claims_worker_before_awaiting_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AccountPool()
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class TerminalWorker:
        state = WorkerState.ERROR

        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1
            stop_entered.set()
            await release_stop.wait()

    worker = TerminalWorker()
    pool._workers = cast(dict[str, AccountWorker], {"terminal-worker": cast(Any, worker)})

    async def desired_accounts():
        return []

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )

    retirement_task = asyncio.create_task(
        pool._retire_terminal_transport("terminal-worker", cast(Any, worker))
    )
    await stop_entered.wait()

    assert pool.has("terminal-worker") is False
    reconcile = await pool.reconcile_desired_accounts()
    assert reconcile == {"started": [], "stopped": []}
    assert worker.stop_calls == 1

    release_stop.set()
    await retirement_task
    assert worker.stop_calls == 1


@pytest.mark.asyncio
async def test_terminal_retirement_skips_worker_already_claimed_by_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AccountPool()
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class TerminalWorker:
        state = WorkerState.ERROR

        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1
            stop_entered.set()
            await release_stop.wait()

    worker = TerminalWorker()
    pool._workers = cast(dict[str, AccountWorker], {"terminal-worker": cast(Any, worker)})

    async def desired_accounts():
        return []

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )

    reconcile_task = asyncio.create_task(pool.reconcile_desired_accounts())
    await stop_entered.wait()
    assert pool.has("terminal-worker") is False

    await pool._retire_terminal_transport("terminal-worker", cast(Any, worker))
    assert worker.stop_calls == 1

    release_stop.set()
    reconcile = await reconcile_task
    assert reconcile == {"started": [], "stopped": ["terminal-worker"]}
    assert worker.stop_calls == 1


@pytest.mark.asyncio
async def test_failed_retirement_claim_blocks_duplicate_ensure_stop() -> None:
    pool = AccountPool()
    fail_closed_entered = asyncio.Event()
    release_fail_closed = asyncio.Event()

    class FailedWorker:
        state = WorkerState.ERROR

        def __init__(self) -> None:
            self.fail_closed_calls = 0
            self.stop_calls = 0

        async def _fail_closed_transport_after_lifecycle_error(
            self,
            _error: Exception,
        ) -> None:
            self.fail_closed_calls += 1
            fail_closed_entered.set()
            await release_fail_closed.wait()

        async def stop(self) -> None:
            self.stop_calls += 1

    worker = FailedWorker()
    account_id = "failed-retirement"
    pool._workers = cast(dict[str, AccountWorker], {account_id: cast(Any, worker)})

    retirement_task = asyncio.create_task(
        pool._retire_failed_transport(account_id, cast(Any, worker), RuntimeError("boom"))
    )
    await fail_closed_entered.wait()

    assert pool.has(account_id) is False
    assert account_id in pool._retirement_barriers
    ensure_task = asyncio.create_task(pool.ensure_stopped(account_id))
    await asyncio.sleep(0)
    assert ensure_task.done() is False
    assert worker.fail_closed_calls == 1
    assert worker.stop_calls == 0

    release_fail_closed.set()
    await retirement_task
    assert await ensure_task == "already_stopped"
    assert worker.fail_closed_calls == 1
    assert worker.stop_calls == 0
    assert account_id not in pool._retirement_barriers


@pytest.mark.asyncio
async def test_stop_all_waits_for_claimed_lifecycle_cleanup() -> None:
    pool = AccountPool()
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class TerminalWorker:
        state = WorkerState.ERROR

        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1
            stop_entered.set()
            await release_stop.wait()

    account_id = "shutdown-retirement"
    worker = TerminalWorker()
    pool._workers = cast(dict[str, AccountWorker], {account_id: cast(Any, worker)})
    cleanup = asyncio.create_task(
        pool._retire_terminal_transport(account_id, cast(Any, worker))
    )
    pool._lifecycle_cleanup_tasks.add(cleanup)
    cleanup.add_done_callback(pool._lifecycle_cleanup_tasks.discard)

    await stop_entered.wait()
    assert pool.has(account_id) is False
    assert account_id in pool._retirement_barriers

    stop_all_task = asyncio.create_task(pool.stop_all())
    await asyncio.sleep(0)
    assert stop_all_task.done() is False

    release_stop.set()
    await stop_all_task
    assert cleanup.done() is True
    assert worker.stop_calls == 1
    assert pool._lifecycle_cleanup_tasks == set()
    assert pool._retirement_barriers == {}


@pytest.mark.asyncio
async def test_reconcile_waits_for_retirement_before_replacement_and_preserves_write_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AccountPool()
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()
    writes: list[str] = []
    built: list[str] = []

    class OldWorker:
        state = WorkerState.ERROR

        async def stop(self) -> None:
            writes.append("old_stopping")
            stop_entered.set()
            await release_stop.wait()
            writes.append("old_disabled")

    class ReplacementWorker:
        state = WorkerState.DISABLED
        lifecycle_error = None
        _stop_requested = False

        def __init__(self) -> None:
            self._client = SimpleNamespace(_task=None)

        def start(self) -> None:
            writes.append("new_online")

    account_id = "generation-barrier"
    old_worker = OldWorker()
    pool._workers = cast(dict[str, AccountWorker], {account_id: cast(Any, old_worker)})

    async def desired_accounts():
        return [SimpleNamespace(account_id=account_id)]

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )

    def build_replacement(replacement_id: str) -> AccountWorker:
        built.append(replacement_id)
        return cast(Any, ReplacementWorker())

    pool._worker_factory = build_replacement
    retirement_task = asyncio.create_task(
        pool._retire_terminal_transport(account_id, cast(Any, old_worker))
    )
    await stop_entered.wait()

    first_reconcile = await pool.reconcile_desired_accounts()
    assert first_reconcile == {"started": [], "stopped": []}
    assert built == []
    assert writes == ["old_stopping"]
    assert pool.ensure_started(account_id) == "retiring"
    assert built == []

    release_stop.set()
    await retirement_task
    assert writes == ["old_stopping", "old_disabled"]

    second_reconcile = await pool.reconcile_desired_accounts()
    assert second_reconcile == {"started": [account_id], "stopped": []}
    assert built == [account_id]
    assert writes == ["old_stopping", "old_disabled", "new_online"]
    assert writes[-1] == "new_online"


@pytest.mark.asyncio
async def test_restart_waits_for_existing_retirement_barrier() -> None:
    pool = AccountPool()
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()
    built: list[str] = []

    class OldWorker:
        state = WorkerState.ERROR

        async def stop(self) -> None:
            stop_entered.set()
            await release_stop.wait()

    class ReplacementWorker:
        state = WorkerState.DISABLED
        lifecycle_error = None
        _stop_requested = False

        def __init__(self) -> None:
            self._client = SimpleNamespace(_task=None)

        def start(self) -> None:
            return None

    account_id = "restart-generation-barrier"
    old_worker = OldWorker()
    pool._workers = cast(dict[str, AccountWorker], {account_id: cast(Any, old_worker)})

    def build_replacement(replacement_id: str) -> AccountWorker:
        built.append(replacement_id)
        return cast(Any, ReplacementWorker())

    pool._worker_factory = build_replacement
    retirement_task = asyncio.create_task(
        pool._retire_terminal_transport(account_id, cast(Any, old_worker))
    )
    await stop_entered.wait()

    restart_task = asyncio.create_task(pool.restart_worker(account_id))
    await asyncio.sleep(0)
    assert restart_task.done() is False
    assert built == []

    release_stop.set()
    await retirement_task
    assert await restart_task == "restarted"
    assert built == [account_id]
    assert pool.has(account_id) is True


@pytest.mark.asyncio
async def test_slow_retirement_does_not_block_other_account_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = AccountPool()
    slow_entered = asyncio.Event()
    release_slow = asyncio.Event()
    built: list[str] = []

    class RetiringWorker:
        state = WorkerState.ERROR

        def __init__(self, *, slow: bool) -> None:
            self.slow = slow
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1
            if self.slow:
                slow_entered.set()
                await release_slow.wait()

    class ReplacementWorker:
        state = WorkerState.DISABLED
        lifecycle_error = None
        _stop_requested = False

        def __init__(self) -> None:
            self._client = SimpleNamespace(_task=None)

        def start(self) -> None:
            return None

    slow_id = "a-slow-retirement"
    fast_id = "b-fast-retirement"
    slow_worker = RetiringWorker(slow=True)
    fast_worker = RetiringWorker(slow=False)
    pool._workers = cast(
        dict[str, AccountWorker],
        {
            slow_id: cast(Any, slow_worker),
            fast_id: cast(Any, fast_worker),
        },
    )

    async def desired_accounts():
        return [
            SimpleNamespace(account_id=slow_id),
            SimpleNamespace(account_id=fast_id),
        ]

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )

    def build_replacement(account_id: str) -> AccountWorker:
        built.append(account_id)
        return cast(Any, ReplacementWorker())

    pool._worker_factory = build_replacement
    slow_task = asyncio.create_task(
        pool._retire_terminal_transport(slow_id, cast(Any, slow_worker))
    )
    fast_task = asyncio.create_task(
        pool._retire_terminal_transport(fast_id, cast(Any, fast_worker))
    )
    await slow_entered.wait()
    await fast_task

    assert slow_id in pool._retirement_barriers
    assert fast_id not in pool._retirement_barriers
    reconcile = await pool.reconcile_desired_accounts()
    assert reconcile == {"started": [fast_id], "stopped": []}
    assert built == [fast_id]
    assert pool.has(fast_id) is True
    assert pool.has(slow_id) is False
    assert slow_worker.stop_calls == 1
    assert fast_worker.stop_calls == 1

    release_slow.set()
    await slow_task
    reconcile = await pool.reconcile_desired_accounts()
    assert reconcile == {"started": [slow_id], "stopped": []}
    assert built == [fast_id, slow_id]
    assert pool.has(slow_id) is True
