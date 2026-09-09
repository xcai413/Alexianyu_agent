"""Focused regressions for AccountWorker lifecycle persistence ownership and races."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import WorkerStatus, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.runtime.worker_state import WorkerState, transition_worker_state
from xianyu_agent.protocol.client import WsClient
from xianyu_agent.protocol.events import ConnectionState, ConnectionStateChanged
from xianyu_agent.protocol.ws.sync import SubscriptionReady
from xianyu_agent.runtime.account_worker import AccountWorker
from xianyu_agent.runtime.recovery import RecoverySupervisor

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
Predicate = Callable[[], bool]


class FakeClient:
    """Minimal transport surface for deterministic lifecycle interleavings."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.state = ConnectionState.IDLE
        self.subscription_ready: SubscriptionReady | None = None
        self.on_state: Callable[[ConnectionStateChanged], Awaitable[None]] | None = None
        self.on_auth_failure = None
        self.on_event = None

    async def emit(self, state: ConnectionState, detail: str | None = None) -> None:
        self.state = state
        if self.on_state is None:
            return
        await self.on_state(
            ConnectionStateChanged(
                event_id=uuid.uuid4().hex,
                account_id=self.account_id,
                state=state,
                detail=detail,
                occurred_at=NOW,
            )
        )

    def start(self) -> None:
        raise AssertionError("focused serialization tests must not start a real transport")

    async def stop(self) -> None:
        return None

    async def send_text(self, _text: str) -> bool:
        return False


class UnusedCredentials:
    """Recovery dependency that must never be invoked by these transport-only tests."""

    async def ensure(self, _account_id: str) -> Any:
        raise AssertionError("credential ensure is outside these regression scenarios")

    async def refresh(self, _account_id: str, *, validation_recovery: bool = False) -> Any:
        del validation_recovery
        raise AssertionError("credential refresh is outside these regression scenarios")


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "worker-lifecycle-serialization.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
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


async def advance_to_connecting(worker: AccountWorker) -> None:
    await worker._set_worker_state(WorkerState.STARTING)
    await worker._set_worker_state(WorkerState.CHECKING_SESSION)
    await worker._set_worker_state(WorkerState.CONNECTING)


@pytest.mark.asyncio
async def test_worker_owned_ws_transport_never_writes_legacy_connected(clean_db) -> None:
    account_id = "owned-ws"
    await domain_accounts.create_account(account_id, enabled=True)
    worker = AccountWorker(account_id, persist_events=False, automation_mode="passive")

    await advance_to_connecting(worker)
    assert worker.state is WorkerState.CONNECTING
    assert await persisted_status(account_id) == "connecting"
    assert worker._client.__class__ is not WsClient

    # The transport diagnostics hook runs before AccountWorker's state callback.
    # It must leave the canonical lifecycle column untouched, eliminating the
    # historical transient/terminal legacy "connected" write.
    await worker._client._update_worker_status(
        state=ConnectionState.CONNECTED,
        detail=None,
    )
    after_transport_diagnostics = await persisted_status(account_id)
    assert after_transport_diagnostics == "connecting"

    await worker._client._emit_state(ConnectionState.CONNECTED)
    assert worker.state is WorkerState.SYNCING
    after_connected_callback = await persisted_status(account_id)
    assert after_connected_callback == "syncing"
    assert WorkerState.ONLINE not in worker.state_history

    worker._client._subscription_ready = SubscriptionReady(sync_type=1, state_body={"pts": 1})
    await wait_until(lambda: worker.state is WorkerState.ONLINE)
    after_subscription_ready = await persisted_status(account_id)
    assert after_subscription_ready == "online"

    assert "connected" not in {
        after_transport_diagnostics,
        after_connected_callback,
        after_subscription_ready,
    }


@pytest.mark.asyncio
async def test_standalone_ws_client_keeps_legacy_connected_writer_compatibility(clean_db) -> None:
    account_id = "standalone-ws"
    account = await domain_accounts.create_account(account_id, enabled=True)
    async with get_async_session() as session:
        session.add(WorkerStatus(account_id=account.id, status="connecting"))
        await session.commit()

    client = WsClient(account_id)
    await client._update_worker_status(state=ConnectionState.CONNECTED, detail=None)

    # AccountWorker owns canonical persistence only for the client it creates;
    # standalone WsClient keeps the released compatibility contract unchanged.
    assert await persisted_status(account_id) == "connected"


@pytest.mark.asyncio
async def test_readiness_and_reconnect_persistence_are_serialized(
    clean_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = "readiness-race"
    await domain_accounts.create_account(account_id, enabled=True)
    client = FakeClient(account_id)
    credentials = UnusedCredentials()
    worker = AccountWorker(
        account_id,
        client=client,  # type: ignore[arg-type]
        credential_supervisor=credentials,  # type: ignore[arg-type]
        recovery_supervisor=RecoverySupervisor(credentials),  # type: ignore[arg-type]
        readiness_poll_s=0.001,
        persist_events=False,
        automation_mode="passive",
    )

    await advance_to_connecting(worker)
    await client.emit(ConnectionState.CONNECTED)
    assert worker.state is WorkerState.SYNCING
    assert await persisted_status(account_id) == "syncing"
    await worker._cancel_readiness_monitor()

    original_persist = worker._persist_worker_state
    online_persist_entered = asyncio.Event()
    allow_online_persist = asyncio.Event()
    reconnect_persist_entered = asyncio.Event()
    allow_reconnect_persist = asyncio.Event()

    async def gated_persist(state: WorkerState, *, detail: str | None = None) -> None:
        if state is WorkerState.ONLINE:
            online_persist_entered.set()
            await allow_online_persist.wait()
        elif state is WorkerState.RECONNECTING:
            reconnect_persist_entered.set()
            await allow_reconnect_persist.wait()
        await original_persist(state, detail=detail)

    monkeypatch.setattr(worker, "_persist_worker_state", gated_persist)

    # Readiness wins the lock first but is deliberately paused before its DB
    # commit. A reconnect arrives while it is paused and must wait for the same
    # transition lock rather than compute a stale transition concurrently.
    client.subscription_ready = SubscriptionReady(sync_type=1, state_body={"pts": 7})
    readiness_task = asyncio.create_task(worker._watch_subscription_ready())
    await online_persist_entered.wait()

    assert worker.state is WorkerState.SYNCING
    assert await persisted_status(account_id) == "syncing"

    reconnect_task = asyncio.create_task(client.emit(ConnectionState.RECONNECTING, "race"))
    await wait_until(lambda: client.state is ConnectionState.RECONNECTING)
    await asyncio.sleep(0)
    assert reconnect_task.done() is False
    assert worker.state is WorkerState.SYNCING
    assert await persisted_status(account_id) == "syncing"

    allow_online_persist.set()
    await reconnect_persist_entered.wait()

    # ONLINE committed and memory/history advanced as one serialized operation
    # before RECONNECTING is allowed to commit. No observable cross-order pair
    # exists at the hand-off boundary.
    assert worker.state is WorkerState.ONLINE
    assert await persisted_status(account_id) == "online"

    allow_reconnect_persist.set()
    await asyncio.gather(readiness_task, reconnect_task)

    assert worker.state is WorkerState.RECONNECTING
    assert await persisted_status(account_id) == "reconnecting"

    # The ready marker is stale after disconnect/reconnect. Re-running the
    # readiness watcher must not promote a non-SYNCING/non-CONNECTED worker.
    await worker._watch_subscription_ready()
    assert worker.state is WorkerState.RECONNECTING
    assert await persisted_status(account_id) == "reconnecting"

    assert worker.state_history == (
        WorkerState.DISABLED,
        WorkerState.STARTING,
        WorkerState.CHECKING_SESSION,
        WorkerState.CONNECTING,
        WorkerState.REGISTERING,
        WorkerState.SYNCING,
        WorkerState.ONLINE,
        WorkerState.RECONNECTING,
    )
    for current, target in zip(worker.state_history, worker.state_history[1:]):
        assert transition_worker_state(current, target) is target
