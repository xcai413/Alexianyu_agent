"""Regression coverage for AccountWorker canonical lifecycle integration."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from xianyu_agent.application.session.health import (
    CredentialHandle,
    CredentialHealth,
    CredentialHealthState,
    CredentialResult,
    CredentialResultState,
)
from xianyu_agent.application.session.ports import CredentialFailureCode
from xianyu_agent.config import get_settings, reset_settings_cache
from xianyu_agent.db import WorkerStatus, database as db_mod, get_async_session
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.runtime.worker_state import (
    InvalidWorkerStateTransition,
    WorkerState,
)
from xianyu_agent.protocol.events import ConnectionState, ConnectionStateChanged, WsFrame
from xianyu_agent.protocol.ws.sync import SubscriptionReady
from xianyu_agent.protocol.ws_auth import WsAuthError
from xianyu_agent.runtime.account_lock import AccountConnectionLock
from xianyu_agent.runtime.account_pool import AccountPool
from xianyu_agent.runtime.account_worker import AccountWorker
from xianyu_agent.runtime.recovery import RecoverySupervisor

NOW = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
Predicate = Callable[[], bool]


class FakeClient:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.state = ConnectionState.IDLE
        self.subscription_ready: SubscriptionReady | None = None
        self.on_state: Callable[[ConnectionStateChanged], Awaitable[None]] | None = None
        self.on_auth_failure = None
        self.on_event = None
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    async def stop(self) -> None:
        self.stop_calls += 1
        await self.emit(ConnectionState.DISCONNECTED, "stopped")

    async def send_text(self, _text: str) -> bool:
        return self.state is ConnectionState.CONNECTED and self.subscription_ready is not None

    def inject_frame(self, _frame: Any) -> None:
        return None

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


class FakeCredentials:
    def __init__(
        self,
        account_id: str,
        *,
        ensure_results: list[CredentialResult[str]],
        refresh_results: list[CredentialResult[str]] | None = None,
        validation_results: list[CredentialResult[str]] | None = None,
    ) -> None:
        self.account_id = account_id
        self.ensure_results = list(ensure_results)
        self.refresh_results = list(refresh_results or [])
        self.validation_results = list(validation_results or [])
        self.ensure_calls = 0
        self.refresh_calls: list[bool] = []

    async def ensure(self, account_id: str) -> CredentialResult[str]:
        assert account_id == self.account_id
        self.ensure_calls += 1
        return self.ensure_results.pop(0)

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[str]:
        assert account_id == self.account_id
        self.refresh_calls.append(validation_recovery)
        queue = self.validation_results if validation_recovery else self.refresh_results
        return queue.pop(0)


def success(account_id: str, *, refreshed: bool = False) -> CredentialResult[str]:
    return CredentialResult(
        state=CredentialResultState.SUCCESS,
        health=CredentialHealth(account_id=account_id, state=CredentialHealthState.HEALTHY),
        credential=CredentialHandle("secret-material"),
        refreshed=refreshed,
    )


def retryable(account_id: str) -> CredentialResult[str]:
    return CredentialResult(
        state=CredentialResultState.RETRYABLE_FAILURE,
        health=CredentialHealth(account_id=account_id, state=CredentialHealthState.HEALTHY),
        code=CredentialFailureCode.NETWORK_ERROR.value,
    )


def needs_validation(account_id: str) -> CredentialResult[str]:
    return CredentialResult(
        state=CredentialResultState.NEEDS_VALIDATION,
        health=CredentialHealth(
            account_id=account_id,
            state=CredentialHealthState.NEEDS_VALIDATION,
        ),
        code=CredentialFailureCode.NEEDS_VALIDATION.value,
    )


def terminal(account_id: str) -> CredentialResult[str]:
    return CredentialResult(
        state=CredentialResultState.TERMINAL_FAILURE,
        health=CredentialHealth(account_id=account_id, state=CredentialHealthState.UNUSABLE),
        code=CredentialFailureCode.IDENTITY_MISSING.value,
    )


def make_worker(
    account_id: str = "acc-1",
    *,
    client: FakeClient | None = None,
    credentials: FakeCredentials | None = None,
    retry_sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[AccountWorker, FakeClient, FakeCredentials]:
    fake_client = client or FakeClient(account_id)
    fake_credentials = credentials or FakeCredentials(
        account_id,
        ensure_results=[success(account_id)],
    )

    async def no_wait(_delay: float) -> None:
        await asyncio.sleep(0)

    worker = AccountWorker(
        account_id,
        client=fake_client,  # type: ignore[arg-type]
        credential_supervisor=fake_credentials,  # type: ignore[arg-type]
        recovery_supervisor=RecoverySupervisor(fake_credentials),
        retry_sleep=retry_sleep or no_wait,
        readiness_poll_s=0.001,
        persist_events=False,
        automation_mode="passive",
    )
    return worker, fake_client, fake_credentials


async def wait_until(predicate: Predicate) -> None:
    for _ in range(5000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise TimeoutError("condition did not become true")


async def bring_online(worker: AccountWorker, client: FakeClient) -> None:
    worker.start()
    await wait_until(lambda: client.start_calls == 1)
    assert worker.state is WorkerState.CONNECTING
    await client.emit(ConnectionState.CONNECTING)
    await client.emit(ConnectionState.CONNECTED)
    assert worker.state is WorkerState.SYNCING
    client.subscription_ready = SubscriptionReady(sync_type=1, state_body={"pts": 1})
    await wait_until(lambda: worker.state is WorkerState.ONLINE)


@pytest.mark.asyncio
async def test_normal_start_requires_subscription_ready_before_online() -> None:
    worker, client, credentials = make_worker()
    worker.start()
    await wait_until(lambda: client.start_calls == 1)

    assert credentials.ensure_calls == 1
    assert worker.state is WorkerState.CONNECTING
    await client.emit(ConnectionState.CONNECTING)
    await client.emit(ConnectionState.CONNECTED)
    assert worker.state is WorkerState.SYNCING
    assert WorkerState.ONLINE not in worker.state_history

    client.subscription_ready = SubscriptionReady(sync_type=1, state_body={"pts": 7})
    await wait_until(lambda: worker.state is WorkerState.ONLINE)
    assert worker.state_history[-4:] == (
        WorkerState.CONNECTING,
        WorkerState.REGISTERING,
        WorkerState.SYNCING,
        WorkerState.ONLINE,
    )


@pytest.mark.asyncio
async def test_disconnect_reconnects_through_canonical_intermediate_states() -> None:
    worker, client, _ = make_worker()
    await bring_online(worker, client)

    client.subscription_ready = None
    await client.emit(ConnectionState.RECONNECTING, "network")
    assert worker.state is WorkerState.RECONNECTING
    await client.emit(ConnectionState.CONNECTING)
    assert worker.state is WorkerState.CONNECTING
    await client.emit(ConnectionState.CONNECTED)
    assert worker.state is WorkerState.SYNCING

    client.subscription_ready = SubscriptionReady(sync_type=2, state_body={"pts": 9})
    await wait_until(lambda: worker.state is WorkerState.ONLINE)
    assert worker.state_history[-5:] == (
        WorkerState.RECONNECTING,
        WorkerState.CONNECTING,
        WorkerState.REGISTERING,
        WorkerState.SYNCING,
        WorkerState.ONLINE,
    )


@pytest.mark.asyncio
async def test_credential_ensure_refresh_is_reflected_in_worker_state() -> None:
    credentials = FakeCredentials(
        "acc-1",
        ensure_results=[success("acc-1", refreshed=True)],
    )
    worker, client, _ = make_worker(credentials=credentials)
    worker.start()
    await wait_until(lambda: client.start_calls == 1)

    assert worker.state is WorkerState.CONNECTING
    assert worker.state_history[:6] == (
        WorkerState.DISABLED,
        WorkerState.STARTING,
        WorkerState.CHECKING_SESSION,
        WorkerState.REFRESHING_CREDENTIAL,
        WorkerState.CHECKING_SESSION,
        WorkerState.CONNECTING,
    )


@pytest.mark.asyncio
async def test_auth_failure_uses_refresh_contract_and_returns_to_session_check() -> None:
    credentials = FakeCredentials(
        "acc-1",
        ensure_results=[success("acc-1")],
        refresh_results=[success("acc-1", refreshed=True)],
    )
    worker, client, _ = make_worker(credentials=credentials)
    await bring_online(worker, client)

    stop_retry = await worker._on_auth_failure(WsAuthError("AUTH_FAILED"))

    assert stop_retry is False
    assert credentials.refresh_calls == [False]
    assert worker.state is WorkerState.CHECKING_SESSION
    assert worker.state_history[-3:] == (
        WorkerState.RECONNECTING,
        WorkerState.REFRESHING_CREDENTIAL,
        WorkerState.CHECKING_SESSION,
    )


@pytest.mark.asyncio
async def test_retryable_credential_failure_retries_same_ensure_route() -> None:
    delays: list[float] = []

    async def capture_delay(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    credentials = FakeCredentials(
        "acc-1",
        ensure_results=[retryable("acc-1"), success("acc-1")],
    )
    worker, client, _ = make_worker(credentials=credentials, retry_sleep=capture_delay)
    worker.start()
    await wait_until(lambda: client.start_calls == 1)

    assert credentials.ensure_calls == 2
    assert delays == [2.0]
    assert worker.state is WorkerState.CONNECTING


@pytest.mark.asyncio
async def test_needs_validation_stops_only_that_account_and_never_auto_retries() -> None:
    blocked_credentials = FakeCredentials(
        "blocked",
        ensure_results=[needs_validation("blocked")],
    )
    blocked, blocked_client, _ = make_worker(
        "blocked",
        client=FakeClient("blocked"),
        credentials=blocked_credentials,
    )
    healthy, healthy_client, _ = make_worker(
        "healthy",
        client=FakeClient("healthy"),
        credentials=FakeCredentials("healthy", ensure_results=[success("healthy")]),
    )

    blocked.start()
    healthy.start()
    await wait_until(lambda: blocked.state is WorkerState.NEEDS_VALIDATION)
    await wait_until(lambda: healthy_client.start_calls == 1)

    assert blocked_client.start_calls == 0
    assert blocked_credentials.ensure_calls == 1
    blocked.start()
    await asyncio.sleep(0)
    assert blocked_credentials.ensure_calls == 1
    assert healthy.state is WorkerState.CONNECTING

    await healthy_client.emit(ConnectionState.CONNECTED)
    healthy_client.subscription_ready = SubscriptionReady(sync_type=1, state_body={})
    await wait_until(lambda: healthy.state is WorkerState.ONLINE)
    assert blocked.state is WorkerState.NEEDS_VALIDATION


@pytest.mark.asyncio
async def test_validation_recovery_is_explicit_and_single_attempt() -> None:
    credentials = FakeCredentials(
        "acc-1",
        ensure_results=[needs_validation("acc-1")],
        validation_results=[success("acc-1", refreshed=True)],
    )
    worker, client, _ = make_worker(credentials=credentials)
    worker.start()
    await wait_until(lambda: worker.state is WorkerState.NEEDS_VALIDATION)
    assert client.start_calls == 0

    assert await worker.recover_validation() is True
    assert credentials.refresh_calls == [True]
    assert worker.state is WorkerState.CONNECTING
    assert client.start_calls == 1


@pytest.mark.asyncio
async def test_terminal_credential_failure_fails_closed_to_error() -> None:
    credentials = FakeCredentials("acc-1", ensure_results=[terminal("acc-1")])
    worker, client, _ = make_worker(credentials=credentials)
    worker.start()
    await wait_until(lambda: worker.state is WorkerState.ERROR)

    assert client.start_calls == 0
    assert credentials.ensure_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [WorkerState.CONNECTING, WorkerState.SYNCING])
async def test_stop_during_connecting_or_syncing_reaches_disabled(phase: WorkerState) -> None:
    worker, client, _ = make_worker()
    worker.start()
    await wait_until(lambda: client.start_calls == 1)
    if phase is WorkerState.SYNCING:
        await client.emit(ConnectionState.CONNECTED)
        assert worker.state is WorkerState.SYNCING
    else:
        assert worker.state is WorkerState.CONNECTING

    await worker.stop()

    assert worker.state is WorkerState.DISABLED
    assert worker.state_history[-2:] == (WorkerState.STOPPING, WorkerState.DISABLED)
    assert client.stop_calls == 1


@pytest.mark.asyncio
async def test_stop_from_online_reaches_stopping_then_disabled() -> None:
    worker, client, _ = make_worker()
    await bring_online(worker, client)
    await worker.stop()

    assert worker.state is WorkerState.DISABLED
    assert worker.state_history[-2:] == (WorkerState.STOPPING, WorkerState.DISABLED)
    assert worker.is_running is False


@pytest.mark.asyncio
async def test_invalid_transition_is_rejected_without_mutating_state() -> None:
    worker, _, _ = make_worker()
    with pytest.raises(InvalidWorkerStateTransition):
        await worker._set_worker_state(WorkerState.ONLINE)

    assert worker.state is WorkerState.DISABLED
    assert worker.state_history == (WorkerState.DISABLED,)


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "worker-lifecycle.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_restart_normalizes_legacy_status_without_promoting_connected_to_online(clean_db) -> None:
    account = await domain_accounts.create_account("acc-1", enabled=True)
    async with get_async_session() as session:
        session.add(WorkerStatus(account_id=account.id, status="connected"))
        await session.commit()

    worker, client, _ = make_worker()
    worker.start()
    await wait_until(lambda: client.start_calls == 1)

    assert worker.restored_state is WorkerState.SYNCING
    assert worker.state is WorkerState.CONNECTING
    await client.emit(ConnectionState.CONNECTED)
    assert worker.state is WorkerState.SYNCING
    row = await domain_accounts.worker_status_for("acc-1")
    assert row is not None
    assert row.status == "connected"

    client.subscription_ready = SubscriptionReady(sync_type=1, state_body={})
    await wait_until(lambda: worker.state is WorkerState.ONLINE)
    row = await domain_accounts.worker_status_for("acc-1")
    assert row is not None
    assert row.status == "online"


@pytest.mark.asyncio
async def test_persisted_validation_gate_blocks_restart_before_credential_ensure(clean_db) -> None:
    account = await domain_accounts.create_account("acc-1", enabled=True)
    async with get_async_session() as session:
        session.add(
            WorkerStatus(
                account_id=account.id,
                status="connected",
                risk_recovery_required=True,
            )
        )
        await session.commit()

    credentials = FakeCredentials("acc-1", ensure_results=[success("acc-1")])
    worker, client, _ = make_worker(credentials=credentials)
    worker.start()
    await wait_until(lambda: worker.state is WorkerState.NEEDS_VALIDATION)

    assert worker.restored_state is WorkerState.NEEDS_VALIDATION
    assert credentials.ensure_calls == 0
    assert client.start_calls == 0


@pytest.mark.asyncio
async def test_synthetic_injection_is_serial_and_preserves_injection_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, _, _ = make_worker()
    active = 0
    max_active = 0
    observed: list[str | None] = []

    async def capture_dispatch(frame: WsFrame) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        observed.append(frame.packet_id)
        active -= 1

    monkeypatch.setattr(worker, "_dispatch_injected_frame", capture_dispatch)
    frames = [
        WsFrame(code=0, packetId=f"p-{index}", headers={}, body={}, received_at=NOW)
        for index in range(1, 4)
    ]
    for frame in frames:
        worker.inject_frame(frame)
    await worker._drain_injected_frames()

    assert observed == ["p-1", "p-2", "p-3"]
    assert max_active == 1


@pytest.mark.asyncio
async def test_synthetic_injection_remains_available_after_terminal_credential_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = FakeCredentials("acc-1", ensure_results=[terminal("acc-1")])
    worker, client, _ = make_worker(credentials=credentials)
    observed: list[str | None] = []

    async def capture_dispatch(frame: WsFrame) -> None:
        observed.append(frame.packet_id)

    monkeypatch.setattr(worker, "_dispatch_injected_frame", capture_dispatch)
    worker.start()
    await wait_until(lambda: worker.state is WorkerState.ERROR)
    assert client.start_calls == 0

    worker.inject_frame(
        WsFrame(code=0, packetId="offline", headers={}, body={}, received_at=NOW)
    )
    await worker._drain_injected_frames()

    assert observed == ["offline"]


@pytest.mark.asyncio
async def test_account_pool_removes_worker_when_connection_lock_is_already_held(clean_db) -> None:
    account_id = "locked-account"
    external_lock = AccountConnectionLock(get_settings().account_lock_path(account_id))
    external_lock.acquire(owner_id="external-test-owner")
    try:
        worker = AccountWorker(account_id, persist_events=False, automation_mode="passive")
        pool = AccountPool()
        pool._workers[account_id] = worker

        assert pool.start_all() == []
        assert pool.has(account_id) is False
        assert worker.state is WorkerState.DISABLED
        assert worker.started_at is None
    finally:
        external_lock.release()


@pytest.mark.asyncio
async def test_worker_releases_reserved_lock_when_credential_gate_fails(clean_db) -> None:
    account_id = "credential-failure"
    credentials = FakeCredentials(account_id, ensure_results=[terminal(account_id)])
    worker = AccountWorker(
        account_id,
        persist_events=False,
        automation_mode="passive",
        credential_supervisor=credentials,  # type: ignore[arg-type]
        recovery_supervisor=RecoverySupervisor(credentials),
    )
    worker.start()
    await wait_until(lambda: worker.state is WorkerState.ERROR)

    probe = AccountConnectionLock(get_settings().account_lock_path(account_id))
    probe.acquire(owner_id="probe-after-terminal")
    probe.release()
