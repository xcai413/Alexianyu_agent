"""Regressions for AccountPool ownership of post-start lifecycle failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import WorkerStatus, database as db_mod, get_async_session
from xianyu_agent.domain.account import state as domain_accounts
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.protocol.events import ConnectionState, ConnectionStateChanged
from xianyu_agent.runtime import account_pool as account_pool_mod
from xianyu_agent.runtime.account_pool import AccountPool
from xianyu_agent.runtime.account_worker import AccountWorker

Predicate = Callable[[], bool]


class StartableFakeClient:
    """Minimal replacement transport used to prove reconcile can retry."""

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


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    previous_engine = db_mod.async_engine
    previous_factory = db_mod.async_session_factory
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "pool-lifecycle-failures.db"))
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


async def wait_until(predicate: Predicate) -> None:
    for _ in range(5000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise TimeoutError("condition did not become true")


async def persisted_status(account_id: str) -> str:
    row = await domain_accounts.worker_status_for(account_id)
    assert row is not None
    return str(row.status)


async def persisted_last_error(account_id: str) -> str | None:
    row = await domain_accounts.worker_status_for(account_id)
    assert row is not None
    return row.last_error


async def drain_pool_lifecycle_cleanup(pool: AccountPool) -> None:
    """Await every pool-owned failure cleanup task scheduled before teardown."""
    while pool._lifecycle_cleanup_tasks:
        tasks = tuple(pool._lifecycle_cleanup_tasks)
        await asyncio.gather(*tasks)


async def start_worker_with_owned_transport(
    account_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AccountPool, AccountWorker, asyncio.Event]:
    await domain_accounts.create_account(account_id, enabled=True)
    worker = AccountWorker(account_id, persist_events=False, automation_mode="passive")
    pool = AccountPool()
    pool._workers[account_id] = worker
    release_transport = asyncio.Event()

    async def transport_lifetime() -> None:
        await release_transport.wait()
        await worker._client._emit_state(ConnectionState.RECONNECTING, "network")

    def start_owned_transport() -> None:
        worker._client._task = asyncio.create_task(
            transport_lifetime(),
            name=f"test-owned-transport-{account_id}",
        )

    monkeypatch.setattr(worker._client, "start", start_owned_transport)
    worker._credentials = None
    startup = pool._start_observed(account_id, worker)
    assert startup is not None
    await startup
    await asyncio.sleep(0)
    assert pool.has(account_id) is True
    assert worker.state is WorkerState.CONNECTING
    assert worker._client._task is not None
    assert pool._transport_tasks.get(account_id) is worker._client._task

    await worker._set_worker_state(WorkerState.REGISTERING)
    await worker._set_worker_state(WorkerState.SYNCING)
    await worker._set_worker_state(WorkerState.ONLINE)
    assert await persisted_status(account_id) == "online"
    return pool, worker, release_transport


async def start_worker_with_normally_ending_transport(
    account_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AccountPool, AccountWorker, asyncio.Event]:
    await domain_accounts.create_account(account_id, enabled=True)
    worker = AccountWorker(account_id, persist_events=False, automation_mode="passive")
    pool = AccountPool()
    pool._workers[account_id] = worker
    release_transport = asyncio.Event()

    async def transport_lifetime() -> None:
        await release_transport.wait()

    def start_owned_transport() -> None:
        worker._client._task = asyncio.create_task(
            transport_lifetime(),
            name=f"test-normal-transport-{account_id}",
        )

    monkeypatch.setattr(worker._client, "start", start_owned_transport)
    worker._credentials = None
    startup = pool._start_observed(account_id, worker)
    assert startup is not None
    await startup
    await asyncio.sleep(0)
    assert worker._client._task is not None
    assert pool._transport_tasks.get(account_id) is worker._client._task
    await worker._set_worker_state(WorkerState.REGISTERING)
    await worker._set_worker_state(WorkerState.SYNCING)
    await worker._set_worker_state(WorkerState.ONLINE)
    return pool, worker, release_transport


async def assert_reconcile_retries_removed_worker(
    pool: AccountPool,
    account_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def desired_accounts():
        return [SimpleNamespace(account_id=account_id)]

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )
    replacements: list[StartableFakeClient] = []

    def build_replacement(worker_account_id: str) -> AccountWorker:
        client = StartableFakeClient(worker_account_id)
        replacements.append(client)
        return AccountWorker(
            worker_account_id,
            client=cast(Any, client),
            persist_events=False,
            automation_mode="passive",
        )

    pool._worker_factory = build_replacement
    result = await pool.reconcile_desired_accounts()
    assert result["started"] == [account_id]
    assert pool.has(account_id) is True
    assert len(replacements) == 1
    assert replacements[0].start_calls == 1
    assert await pool.stop(account_id) is True


@pytest.mark.asyncio
async def test_post_start_callback_failure_retires_worker_and_reconcile_retries(
    clean_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "post-start-transport-failure"
    pool, worker, release_transport = await start_worker_with_owned_transport(
        account_id,
        monkeypatch,
    )
    history_before = worker.state_history

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("forced post-start callback commit failure")

    with monkeypatch.context() as commit_patch:
        commit_patch.setattr(AsyncSession, "commit", fail_commit)
        release_transport.set()
        await wait_until(lambda: worker.lifecycle_error is not None and not pool.has(account_id))
        await drain_pool_lifecycle_cleanup(pool)

        transport_task = worker._client._task
        assert transport_task is not None
        with pytest.raises(RuntimeError, match="forced post-start callback commit failure"):
            await transport_task

        assert isinstance(worker.lifecycle_error, RuntimeError)
        assert str(worker.lifecycle_error) == "forced post-start callback commit failure"
        assert worker._stop_requested is True
        assert worker._client._stop.is_set()
        assert worker.state is WorkerState.ONLINE
        assert worker.state_history == history_before
        assert await persisted_status(account_id) == "online"

    await assert_reconcile_retries_removed_worker(pool, account_id, monkeypatch)
    await drain_pool_lifecycle_cleanup(pool)


@pytest.mark.asyncio
async def test_normal_transport_end_in_error_retires_worker_and_reconcile_retries(
    clean_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "terminal-auth-normal-end"
    pool, worker, release_transport = await start_worker_with_normally_ending_transport(
        account_id,
        monkeypatch,
    )

    await worker._set_worker_state(WorkerState.ERROR, detail="terminal auth failure")
    assert worker.state is WorkerState.ERROR
    assert worker._stop_requested is False
    assert await persisted_status(account_id) == "error"

    transport_task = worker._client._task
    assert transport_task is not None
    release_transport.set()
    await transport_task
    await wait_until(lambda: not pool.has(account_id))
    await drain_pool_lifecycle_cleanup(pool)

    assert worker._stop_requested is True
    assert pool.has(account_id) is False
    await assert_reconcile_retries_removed_worker(pool, account_id, monkeypatch)
    await drain_pool_lifecycle_cleanup(pool)


@pytest.mark.asyncio
async def test_normal_transport_end_in_needs_validation_stays_fail_closed(
    clean_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "needs-validation-normal-end"
    pool, worker, release_transport = await start_worker_with_normally_ending_transport(
        account_id,
        monkeypatch,
    )

    await worker._set_worker_state(WorkerState.NEEDS_VALIDATION, detail="manual validation")
    assert worker.state is WorkerState.NEEDS_VALIDATION
    transport_task = worker._client._task
    assert transport_task is not None
    release_transport.set()
    await transport_task
    await asyncio.sleep(0)

    async def desired_accounts():
        return [SimpleNamespace(account_id=account_id)]

    monkeypatch.setattr(
        account_pool_mod.domain_accounts,
        "list_desired_running_accounts",
        desired_accounts,
    )
    replacements: list[str] = []
    pool._worker_factory = lambda replacement_id: replacements.append(replacement_id)  # type: ignore[assignment,return-value]

    result = await pool.reconcile_desired_accounts()
    assert result["started"] == []
    assert pool.get(account_id) is worker
    assert replacements == []
    await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_error", [None, "live-worker-diagnostic"])
async def test_never_started_explicit_stop_preserves_last_error(
    clean_db,
    existing_error: str | None,
) -> None:
    account_id = f"offline-stop-{existing_error or 'empty'}"
    account = await domain_accounts.create_account(account_id, enabled=True)
    async with get_async_session() as session:
        session.add(
            WorkerStatus(
                account_id=account.id,
                status="online",
                last_error=existing_error,
            )
        )
        await session.commit()

    worker = AccountWorker(account_id, persist_events=False, automation_mode="passive")
    assert worker._client._task is None
    await worker.stop()

    assert worker._client._task is None
    assert await persisted_last_error(account_id) == existing_error


@pytest.mark.asyncio
async def test_stop_all_uses_snapshot_when_worker_map_mutates() -> None:
    pool = AccountPool()
    stopped: list[str] = []

    class MutatingWorker:
        def __init__(self, name: str, on_stop: Callable[[], None] | None = None) -> None:
            self.name = name
            self.on_stop = on_stop

        async def stop(self) -> None:
            stopped.append(self.name)
            if self.on_stop is not None:
                self.on_stop()
            await asyncio.sleep(0)

    second = MutatingWorker("second")
    first = MutatingWorker("first", lambda: pool._workers.pop("second", None))
    pool._workers = cast(
        dict[str, AccountWorker],
        {"first": first, "second": second},
    )

    await pool.stop_all()

    assert stopped == ["first", "second"]
