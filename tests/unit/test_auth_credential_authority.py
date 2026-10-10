"""Regression coverage for canonical credential mutation authority."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

import pytest
import typer
from sqlalchemy import select

from xianyu_agent.application.session import (
    CredentialFailureCode,
    CredentialHandle,
    CredentialHealth,
    CredentialHealthState,
    CredentialResult,
    CredentialResultState,
    CredentialSupervisor,
)
from xianyu_agent.application.session.ports import (
    CredentialBackendError,
    CredentialBackendStatus,
    ValidationStatus,
)
from xianyu_agent.cli.commands import auth as auth_cli, order as order_cli, protocol as protocol_cli
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import AuditLog, database as db_mod, get_async_session
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.protocol.client import ClientConfig, WsClient
from xianyu_agent.protocol.events import ConnectionState
from xianyu_agent.protocol.ws_auth import WsAuthError
from xianyu_agent.runtime.account_lock import (
    AccountConnectionAlreadyRunningError,
    AccountConnectionLock,
)
from xianyu_agent.runtime.account_worker import AccountWorker
from xianyu_agent.runtime.recovery import RecoverySupervisor

ACCOUNT_ID = "cli-credential-account"


@pytest.fixture(autouse=True)
async def isolated_auth_database(tmp_path, monkeypatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "auth.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await auth_cli.domain_accounts.create_account(ACCOUNT_ID)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def test_borrowed_real_file_lock_keeps_outer_ownership_until_cleanup(tmp_path) -> None:
    owner = AccountConnectionLock(tmp_path / "account.lock")
    competitor = AccountConnectionLock(tmp_path / "account.lock")
    owner.acquire(owner_id="cli")
    borrowed = owner.borrow()
    try:
        borrowed.acquire(owner_id="probe")
        borrowed.release()
        with pytest.raises(AccountConnectionAlreadyRunningError):
            competitor.acquire(owner_id="competitor")
    finally:
        owner.release()
    competitor.acquire(owner_id="after-cleanup")
    competitor.release()
    with pytest.raises(RuntimeError, match="released ownership"):
        borrowed.acquire(owner_id="stale-probe")


class _FakeLock:
    def __init__(self, held: dict[str, bool]) -> None:
        self._held = held
        self._held["value"] = True
        self.release_calls = 0

    def release(self) -> None:
        assert self._held["value"] is True
        self.release_calls += 1
        self._held["value"] = False


class _FakeSupervisor:
    def __init__(
        self,
        held: dict[str, bool],
        *,
        health: CredentialHealth,
        result: CredentialResult[object],
    ) -> None:
        self._held = held
        self.health = health
        self.result = result
        self.inspect_calls: list[str] = []
        self.refresh_calls: list[tuple[str, bool]] = []

    async def inspect(self, account_id: str) -> CredentialHealth:
        assert self._held["value"] is True
        self.inspect_calls.append(account_id)
        return self.health

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
        defer_validation_clear: bool = False,
    ) -> CredentialResult[object]:
        assert self._held["value"] is True
        self.refresh_calls.append((account_id, validation_recovery))
        return self.result


def _health(
    state: CredentialHealthState,
    *,
    cooling: bool = False,
) -> CredentialHealth:
    return CredentialHealth(
        account_id=ACCOUNT_ID,
        state=state,
        expires_at=datetime.now(UTC) + timedelta(hours=8)
        if state is CredentialHealthState.HEALTHY
        else None,
        validation_cooling=cooling,
    )


def _result(
    state: CredentialResultState,
    health: CredentialHealth,
    *,
    code: CredentialFailureCode | None = None,
    secret: str | None = None,
) -> CredentialResult[object]:
    return CredentialResult(
        state=state,
        health=health,
        credential=CredentialHandle(object() if secret is None else secret)
        if state is CredentialResultState.SUCCESS
        else None,
        code=code.value if code is not None else None,
        message="safe credential result",
        refreshed=state is CredentialResultState.SUCCESS,
    )


def _install_lock(monkeypatch: pytest.MonkeyPatch, held: dict[str, bool]) -> _FakeLock:
    lock = _FakeLock(held)

    def acquire(account_id: str, operation: str) -> _FakeLock:
        assert account_id == ACCOUNT_ID
        assert operation == "token-refresh"
        return lock

    monkeypatch.setattr(auth_cli, "_acquire_auth_lock", acquire)
    return lock


def test_refresh_uses_supervisor_and_explicit_validation_recovery_under_full_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = {"value": False}
    lock = _install_lock(monkeypatch, held)
    validation_health = _health(CredentialHealthState.NEEDS_VALIDATION)
    healthy = _health(CredentialHealthState.HEALTHY)
    supervisor = _FakeSupervisor(
        held,
        health=validation_health,
        result=_result(CredentialResultState.SUCCESS, healthy),
    )

    async def get_account(account_id: str):
        assert held["value"] is True
        assert account_id == ACCOUNT_ID
        return SimpleNamespace(desired_state="stopped")

    monkeypatch.setattr(auth_cli.domain_accounts, "get_account", get_account)
    monkeypatch.setattr(auth_cli, "_build_credential_supervisor", lambda: supervisor)

    async def probe(
        _account_id: str, _credentials: object, *, timeout_s: float, account_lock: object
    ) -> tuple[bool, str]:
        assert timeout_s == 45.0
        assert account_lock is lock
        owner = await auth_cli.account_lease.live_owner(_account_id)
        assert owner is not None
        assert owner.startswith("auth-recovery:")
        competitor = await auth_cli.account_lease.claim_for_start(
            _account_id, instance_id="competing-recovery"
        )
        assert competitor.acquired is False
        return True, "online"

    monkeypatch.setattr(auth_cli, "_run_validation_recovery_probe", probe)

    auth_cli.refresh(account_id=ACCOUNT_ID)

    assert supervisor.inspect_calls == [ACCOUNT_ID]
    assert supervisor.refresh_calls == []
    assert lock.release_calls == 1
    assert held["value"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_recovery_probe_releases_database_lease_on_exception(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    async def failing_probe(*_args, **_kwargs):
        owner = await auth_cli.account_lease.live_owner(ACCOUNT_ID)
        assert owner is not None
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("probe failed")

    monkeypatch.setattr(auth_cli, "_run_validation_recovery_probe", failing_probe)
    error = asyncio.CancelledError if cancelled else RuntimeError
    with pytest.raises(error):
        await auth_cli._run_leased_validation_probe(
            ACCOUNT_ID, object(), timeout_s=5.0, account_lock=object()
        )
    assert await auth_cli.account_lease.live_owner(ACCOUNT_ID) is None


@pytest.mark.asyncio
async def test_recovery_probe_does_not_run_when_database_lease_is_held(monkeypatch) -> None:
    claim = await auth_cli.account_lease.claim_for_start(ACCOUNT_ID, instance_id="other-instance")
    assert claim.acquired

    async def forbidden_probe(*_args, **_kwargs):
        pytest.fail("must not probe an account owned by another instance")

    monkeypatch.setattr(auth_cli, "_run_validation_recovery_probe", forbidden_probe)
    recovered, reason = await auth_cli._run_leased_validation_probe(
        ACCOUNT_ID, object(), timeout_s=5.0, account_lock=object()
    )
    assert recovered is False
    assert "恢复租约获取失败" in reason
    assert await auth_cli.account_lease.live_owner(ACCOUNT_ID) == "other-instance"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_error", [RuntimeError, TimeoutError, asyncio.CancelledError])
async def test_probe_cleanup_failure_retains_ownership_and_never_audits_success(
    monkeypatch, tmp_path, cleanup_error
):
    class BrokenCleanupWorker:
        state = WorkerState.NEEDS_VALIDATION

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return None

        async def recover_validation(self):
            self.state = WorkerState.ONLINE
            return True

        async def stop(self):
            raise cleanup_error("TOP-SECRET")

    monkeypatch.setattr(auth_cli, "AccountWorker", BrokenCleanupWorker)
    owner = AccountConnectionLock(tmp_path / "cleanup.lock")
    competitor = AccountConnectionLock(tmp_path / "cleanup.lock")
    owner.acquire(owner_id="probe")
    try:
        recovered, reason = await auth_cli._run_leased_validation_probe(
            ACCOUNT_ID, object(), timeout_s=5, account_lock=owner
        )
        assert recovered is False
        assert "TOP-SECRET" not in reason
        assert await auth_cli.account_lease.live_owner(ACCOUNT_ID) is not None
        owner.release()
        with pytest.raises(AccountConnectionAlreadyRunningError):
            competitor.acquire(owner_id="competitor")
        risk = await auth_cli.worker_risk.get(ACCOUNT_ID)
        assert risk is not None
        assert risk.code == "RECOVERY_CLEANUP_FAILED"
        async with get_async_session() as session:
            results = list(
                await session.scalars(
                    select(AuditLog.result).where(AuditLog.action == "ws_validation_recovery_probe")
                )
            )
        assert results == ["failed"]
    finally:
        # No real transport exists in this test; release the quarantined fixture.
        AccountConnectionLock._quarantined.discard(owner)
        owner.release()


@pytest.mark.asyncio
async def test_stuck_cleanup_returns_on_deadline_and_continues_lease_renewal(monkeypatch, tmp_path):
    release = asyncio.Event()
    renewed = asyncio.Event()
    clock = datetime.now(UTC)
    origin = clock
    renew = auth_cli.account_lease.renew

    async def advancing_renew(account_id, *, instance_id, ttl):
        nonlocal clock
        clock += timedelta(seconds=60)
        result = await renew(account_id, instance_id=instance_id, ttl=ttl, now=clock)
        if clock - origin > timedelta(seconds=400):
            renewed.set()
        return result

    class StuckWorker:
        state = WorkerState.NEEDS_VALIDATION

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return None

        async def recover_validation(self):
            self.state = WorkerState.ONLINE
            return True

        async def stop(self):
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

    monkeypatch.setattr(auth_cli, "AccountWorker", StuckWorker)
    monkeypatch.setattr(auth_cli, "_PROBE_CLEANUP_TIMEOUT_S", 0.01)
    monkeypatch.setattr(auth_cli, "_PROBE_LEASE_RENEW_INTERVAL_S", 0.01)
    monkeypatch.setattr(auth_cli.account_lease, "renew", advancing_renew)
    owner = AccountConnectionLock(tmp_path / "stuck.lock")
    owner.acquire(owner_id="probe")
    try:
        recovered, _ = await asyncio.wait_for(
            auth_cli._run_leased_validation_probe(
                ACCOUNT_ID, object(), timeout_s=5, account_lock=owner
            ),
            timeout=1,
        )
        assert recovered is False
        await asyncio.wait_for(renewed.wait(), timeout=1)
        competitor = await auth_cli.account_lease.claim_for_start(
            ACCOUNT_ID, instance_id="other-host", now=clock
        )
        assert competitor.acquired is False
    finally:
        release.set()
        pending = tuple(auth_cli._pending_probe_cleanup)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        guards = tuple(auth_cli._recovery_guards)
        for guard in guards:
            guard.cancel()
        await asyncio.gather(*guards, return_exceptions=True)
        AccountConnectionLock._quarantined.discard(owner)
        owner.release()


@pytest.mark.asyncio
async def test_lease_renewal_loss_aborts_probe_and_preserves_risk_gate(monkeypatch, tmp_path):
    stopped = asyncio.Event()

    class Worker:
        state = WorkerState.NEEDS_VALIDATION

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return None

        async def recover_validation(self):
            await asyncio.Event().wait()

        async def stop(self):
            self.state = WorkerState.DISABLED
            stopped.set()

    async def lost(*_args, **_kwargs):
        return False

    monkeypatch.setattr(auth_cli, "AccountWorker", Worker)
    monkeypatch.setattr(auth_cli.account_lease, "renew", lost)
    owner = AccountConnectionLock(tmp_path / "lost.lock")
    owner.acquire(owner_id="probe")
    try:
        recovered, reason = await auth_cli._run_leased_validation_probe(
            ACCOUNT_ID, object(), timeout_s=5, account_lock=owner
        )
        assert recovered is False
        assert "续租失败" in reason
        assert stopped.is_set()
        assert (await auth_cli.worker_risk.get(ACCOUNT_ID)).code == "RECOVERY_LEASE_LOST"
    finally:
        owner.release()


@pytest.mark.asyncio
async def test_cli_validation_probe_clears_only_after_online_and_stops_probe_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeWorker:
        instances: ClassVar[list[ProbeWorker]] = []

        def __init__(self, *_args, **_kwargs) -> None:
            self.state = WorkerState.NEEDS_VALIDATION
            self.stop_calls = 0
            self.__class__.instances.append(self)

        def start(self):
            return None

        async def recover_validation(self) -> bool:
            self.state = WorkerState.ONLINE
            return True

        async def stop(self) -> None:
            self.stop_calls += 1
            self.state = WorkerState.DISABLED

    monkeypatch.setattr(auth_cli, "AccountWorker", ProbeWorker)

    recovered, reason = await auth_cli._run_validation_recovery_probe(
        ACCOUNT_ID,
        object(),  # type: ignore[arg-type]
        timeout_s=5.0,
    )

    assert recovered is True
    assert reason == "online"
    assert ProbeWorker.instances[0].stop_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_probe_audits_exception_without_secret_and_always_stops(
    monkeypatch, cancelled: bool
) -> None:
    audits = []
    stopped = asyncio.Event()

    class FailingWorker:
        state = WorkerState.NEEDS_VALIDATION

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return None

        async def recover_validation(self):
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("TOP-SECRET credential response")

        async def stop(self):
            stopped.set()

    async def record(_account_id, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(auth_cli, "AccountWorker", FailingWorker)
    monkeypatch.setattr(auth_cli.worker_risk, "record_recovery_probe", record)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await auth_cli._run_validation_recovery_probe(ACCOUNT_ID, object(), timeout_s=5)
    else:
        recovered, reason = await auth_cli._run_validation_recovery_probe(
            ACCOUNT_ID, object(), timeout_s=5
        )
        assert recovered is False
        assert "RuntimeError" in reason
        assert "TOP-SECRET" not in reason
    assert stopped.is_set()
    assert len(audits) == 1
    assert audits[0]["result"] == "failed"
    assert "TOP-SECRET" not in audits[0]["reason"]


@pytest.mark.asyncio
async def test_probe_timeout_cancels_stalled_refresh_and_cleans_worker(monkeypatch) -> None:
    cancelled = asyncio.Event()
    stopped = asyncio.Event()

    class StalledWorker:
        state = WorkerState.NEEDS_VALIDATION

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self):
            return None

        async def recover_validation(self) -> bool:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return False

        async def stop(self) -> None:
            stopped.set()

    monkeypatch.setattr(auth_cli, "AccountWorker", StalledWorker)
    recovered, reason = await asyncio.wait_for(
        auth_cli._run_validation_recovery_probe(ACCOUNT_ID, object(), timeout_s=0.02),
        timeout=1,
    )
    assert recovered is False
    assert "凭据刷新或会话验证" in reason
    assert cancelled.is_set()
    assert stopped.is_set()


def test_refresh_running_worker_fails_closed_before_supervisor_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = {"value": False}
    lock = _install_lock(monkeypatch, held)
    built = False

    async def get_account(account_id: str):
        assert held["value"] is True
        assert account_id == ACCOUNT_ID
        return SimpleNamespace(desired_state="running")

    def forbidden_supervisor():
        nonlocal built
        built = True
        raise AssertionError("running worker must fail before credential mutation")

    monkeypatch.setattr(auth_cli.domain_accounts, "get_account", get_account)
    monkeypatch.setattr(auth_cli, "_build_credential_supervisor", forbidden_supervisor)

    with pytest.raises(typer.Exit) as exc_info:
        auth_cli.refresh(account_id=ACCOUNT_ID)

    assert exc_info.value.exit_code == 2
    assert built is False
    assert lock.release_calls == 1
    assert held["value"] is False


def test_refresh_failure_never_directly_clears_risk_and_never_prints_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = {"value": False}
    _install_lock(monkeypatch, held)
    health = _health(CredentialHealthState.HEALTHY)
    supervisor = _FakeSupervisor(
        held,
        health=health,
        result=_result(
            CredentialResultState.TERMINAL_FAILURE,
            health,
            code=CredentialFailureCode.AUTH_FAILED,
        ),
    )
    output: list[str] = []

    async def get_account(_account_id: str):
        assert held["value"] is True
        return SimpleNamespace(desired_state="stopped")

    async def forbidden_clear(_account_id: str) -> bool:
        raise AssertionError("CLI must not clear validation risk directly")

    monkeypatch.setattr(auth_cli.domain_accounts, "get_account", get_account)
    monkeypatch.setattr(auth_cli.worker_risk, "clear_after_refresh", forbidden_clear)
    monkeypatch.setattr(auth_cli, "_build_credential_supervisor", lambda: supervisor)
    monkeypatch.setattr(
        auth_cli.console, "print", lambda value, **_kwargs: output.append(str(value))
    )

    with pytest.raises(typer.Exit) as exc_info:
        auth_cli.refresh(account_id=ACCOUNT_ID)

    assert exc_info.value.exit_code == 2
    assert supervisor.refresh_calls == [(ACCOUNT_ID, False)]
    rendered = "\n".join(output)
    assert "AUTH_FAILED" in rendered
    assert "TOP-SECRET" not in rendered


def test_live_worker_lock_conflict_prevents_cross_component_refresh_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = False

    def locked(_account_id: str, _operation: str):
        raise typer.Exit(code=2)

    def forbidden_supervisor():
        nonlocal built
        built = True
        raise AssertionError("lifecycle lock conflict must prevent mutation")

    monkeypatch.setattr(auth_cli, "_acquire_auth_lock", locked)
    monkeypatch.setattr(auth_cli, "_build_credential_supervisor", forbidden_supervisor)

    with pytest.raises(typer.Exit) as exc_info:
        auth_cli.refresh(account_id=ACCOUNT_ID)

    assert exc_info.value.exit_code == 2
    assert built is False


class _StandaloneBackend:
    def __init__(
        self,
        status: CredentialBackendStatus,
        *,
        error: CredentialBackendError | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.acquire_calls: list[bool] = []

    async def inspect(self, account_id: str) -> CredentialBackendStatus:
        assert account_id == ACCOUNT_ID
        return self.status

    async def acquire(self, account_id: str, *, force_refresh: bool = False) -> str:
        assert account_id == ACCOUNT_ID
        self.acquire_calls.append(force_refresh)
        if self.error is not None:
            raise self.error
        return "TOP-SECRET-STANDALONE-TOKEN"


class _StandaloneValidation:
    def __init__(self, *, required: bool = False) -> None:
        self.required = required
        self.mark_calls = 0

    async def inspect(self, account_id: str) -> ValidationStatus:
        assert account_id == ACCOUNT_ID
        return ValidationStatus(
            account_id=account_id,
            required=self.required,
            code=CredentialFailureCode.NEEDS_VALIDATION.value if self.required else None,
            revision="test-revision" if self.required else None,
        )

    async def mark_needs_validation(self, account_id: str) -> None:
        assert account_id == ACCOUNT_ID
        self.mark_calls += 1
        self.required = True

    async def clear_after_refresh(self, account_id: str, *, expected_revision=None) -> bool:
        assert account_id == ACCOUNT_ID
        self.required = False
        return True


class _StandaloneHandoffLock:
    def __init__(self) -> None:
        self.held = False
        self.handoff_pending = False
        self.release_calls = 0

    def acquire_for_transport_handoff(self, *, owner_id: str) -> None:
        assert owner_id.startswith("cli:")
        assert self.held is False
        self.held = True
        self.handoff_pending = True

    def acquire(self, *, owner_id: str) -> None:
        assert owner_id.startswith("ws:")
        assert self.held is True
        assert self.handoff_pending is True
        self.handoff_pending = False

    def release(self) -> None:
        assert self.held is True
        self.release_calls += 1
        self.held = False
        self.handoff_pending = False


class _StandaloneClient:
    def __init__(self, lock: _StandaloneHandoffLock) -> None:
        self._lock = lock
        self.start_calls = 0

    def start(self) -> None:
        self._lock.acquire(owner_id=f"ws:{ACCOUNT_ID}:test")
        self.start_calls += 1


async def _invoke_standalone_start(
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
    supervisor: CredentialSupervisor[str] | object,
    client: _StandaloneClient,
    lock: _StandaloneHandoffLock,
) -> None:
    if entrypoint == "redeliver":
        monkeypatch.setattr(order_cli, "_build_credential_supervisor", lambda: supervisor)
        await order_cli._start_with_prepared_credentials(  # type: ignore[arg-type]
            ACCOUNT_ID,
            client,
            lock,
        )
        return

    monkeypatch.setattr(protocol_cli, "_build_credential_supervisor", lambda: supervisor)
    await protocol_cli._start_with_prepared_credentials(  # type: ignore[arg-type]
        ACCOUNT_ID,
        client,
        lock,
        operation=f"protocol-{entrypoint}",
    )


@pytest.mark.parametrize("entrypoint", ["redeliver", "capture", "connect"])
@pytest.mark.asyncio
async def test_standalone_entrypoints_cached_valid_credential_starts_load_only(
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _StandaloneBackend(
        CredentialBackendStatus(
            account_id=ACCOUNT_ID,
            cookie_available=True,
            identity_available=True,
            token_cached=True,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
    )
    supervisor = CredentialSupervisor(backend, _StandaloneValidation())
    lock = _StandaloneHandoffLock()
    client = _StandaloneClient(lock)

    await _invoke_standalone_start(entrypoint, monkeypatch, supervisor, client, lock)

    assert backend.acquire_calls == [False]
    assert client.start_calls == 1
    assert lock.held is True
    assert lock.handoff_pending is False
    assert lock.release_calls == 0


@pytest.mark.parametrize("entrypoint", ["redeliver", "capture", "connect"])
@pytest.mark.parametrize("credential_state", ["missing-current", "expired"])
@pytest.mark.asyncio
async def test_standalone_entrypoints_prepare_missing_or_expired_before_start(
    entrypoint: str,
    credential_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _StandaloneBackend(
        CredentialBackendStatus(
            account_id=ACCOUNT_ID,
            cookie_available=True,
            identity_available=True,
            token_cached=credential_state == "expired",
            expires_at=(datetime.now(UTC) - timedelta(minutes=1))
            if credential_state == "expired"
            else None,
        )
    )
    supervisor = CredentialSupervisor(backend, _StandaloneValidation())
    lock = _StandaloneHandoffLock()
    client = _StandaloneClient(lock)

    await _invoke_standalone_start(entrypoint, monkeypatch, supervisor, client, lock)

    assert backend.acquire_calls == [True]
    assert client.start_calls == 1
    assert lock.held is True
    assert lock.handoff_pending is False
    assert lock.release_calls == 0


@pytest.mark.parametrize("entrypoint", ["redeliver", "capture", "connect"])
@pytest.mark.asyncio
async def test_standalone_entrypoints_needs_validation_fail_closed_before_start(
    entrypoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _StandaloneBackend(
        CredentialBackendStatus(
            account_id=ACCOUNT_ID,
            cookie_available=True,
            identity_available=True,
            token_cached=True,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
    )
    supervisor = CredentialSupervisor(backend, _StandaloneValidation(required=True))
    lock = _StandaloneHandoffLock()
    client = _StandaloneClient(lock)

    with pytest.raises(typer.Exit) as exc_info:
        await _invoke_standalone_start(entrypoint, monkeypatch, supervisor, client, lock)

    assert exc_info.value.exit_code == 2
    assert backend.acquire_calls == []
    assert client.start_calls == 0
    assert lock.held is False
    assert lock.release_calls == 1


@pytest.mark.parametrize("entrypoint", ["redeliver", "capture", "connect"])
@pytest.mark.parametrize(
    ("error", "expected_state"),
    [
        (
            CredentialBackendError(
                CredentialFailureCode.NETWORK_ERROR,
                retryable_hint=True,
            ),
            CredentialResultState.RETRYABLE_FAILURE,
        ),
        (
            CredentialBackendError(CredentialFailureCode.AUTH_FAILED),
            CredentialResultState.TERMINAL_FAILURE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_standalone_entrypoints_preparation_failure_never_starts_transport(
    entrypoint: str,
    error: CredentialBackendError,
    expected_state: CredentialResultState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _StandaloneBackend(
        CredentialBackendStatus(
            account_id=ACCOUNT_ID,
            cookie_available=True,
            identity_available=True,
            token_cached=False,
            expires_at=None,
        ),
        error=error,
    )
    supervisor = CredentialSupervisor(backend, _StandaloneValidation())
    lock = _StandaloneHandoffLock()
    client = _StandaloneClient(lock)
    output: list[str] = []
    module = order_cli if entrypoint == "redeliver" else protocol_cli
    monkeypatch.setattr(
        module.console,
        "print",
        lambda value, **_kwargs: output.append(str(value)),
    )

    with pytest.raises(typer.Exit) as exc_info:
        await _invoke_standalone_start(entrypoint, monkeypatch, supervisor, client, lock)

    assert exc_info.value.exit_code == 2
    assert backend.acquire_calls == [True]
    assert client.start_calls == 0
    assert lock.held is False
    assert lock.release_calls == 1
    rendered = "\n".join(output)
    assert expected_state.value.split("_")[0].lower() not in rendered.lower()
    assert "TOP-SECRET-STANDALONE-TOKEN" not in rendered
    assert "cookie=" not in rendered.lower()


class _LongLivedStandaloneClient:
    def __init__(
        self,
        *_args,
        on_state=None,
        on_error=None,
        on_auth_failure=None,
        **_kwargs,
    ) -> None:
        self.on_state = on_state
        self.on_error = on_error
        self.on_auth_failure = on_auth_failure
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


def _healthy_standalone_backend() -> _StandaloneBackend:
    return _StandaloneBackend(
        CredentialBackendStatus(
            account_id=ACCOUNT_ID,
            cookie_available=True,
            identity_available=True,
            token_cached=True,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
    )


def test_protocol_connect_refreshes_after_auth_failure_and_allows_transport_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _healthy_standalone_backend()
    supervisor = CredentialSupervisor(backend, _StandaloneValidation())
    settings = SimpleNamespace(
        ws_url="wss://unit.test/ws",
        account_lock_path=lambda _account_id: "unit-account.lock",
    )
    clients: list[_LongLivedStandaloneClient] = []
    retry_decisions: list[bool] = []

    def build_client(*args, **kwargs):
        client = _LongLivedStandaloneClient(*args, **kwargs)
        clients.append(client)
        return client

    async def start_with_prepare(
        account_id,
        client,
        _account_lock,
        *,
        operation,
        supervisor,
    ) -> None:
        assert account_id == ACCOUNT_ID
        assert operation == "protocol-connect"
        result = await supervisor.ensure(account_id)
        assert result.state is CredentialResultState.SUCCESS
        assert client.on_auth_failure is not None
        retry_decisions.append(await client.on_auth_failure(WsAuthError("expired")))

    monotonic_values = iter([0.0, 2.0])
    monkeypatch.setattr(protocol_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(protocol_cli, "_build_credential_supervisor", lambda: supervisor)
    monkeypatch.setattr(protocol_cli, "AccountConnectionLock", lambda _path: object())
    monkeypatch.setattr(protocol_cli, "WsClient", build_client)
    monkeypatch.setattr(protocol_cli, "_start_with_prepared_credentials", start_with_prepare)
    monkeypatch.setattr(
        protocol_cli,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )
    monkeypatch.setattr(protocol_cli.console, "print", lambda *_args, **_kwargs: None)

    protocol_cli.connect(account_id=ACCOUNT_ID, seconds=1.0, quiet=True)

    assert backend.acquire_calls == [False, True]
    assert retry_decisions == [False]
    assert clients[0].stop_calls == 1


def test_protocol_capture_recoverable_ws_auth_does_not_finish_before_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    backend = _healthy_standalone_backend()
    supervisor = CredentialSupervisor(backend, _StandaloneValidation())
    settings = SimpleNamespace(
        ws_url="wss://unit.test/ws",
        data_dir=tmp_path,
        account_lock_path=lambda _account_id: "unit-account.lock",
    )
    clients: list[_LongLivedStandaloneClient] = []
    retry_decisions: list[bool] = []
    output: list[str] = []

    async def get_account(account_id: str):
        assert account_id == ACCOUNT_ID
        return SimpleNamespace(desired_state="stopped")

    def build_client(*args, **kwargs):
        client = _LongLivedStandaloneClient(*args, **kwargs)
        clients.append(client)
        return client

    async def start_with_prepare(
        account_id,
        client,
        _account_lock,
        *,
        operation,
        supervisor,
    ) -> None:
        assert account_id == ACCOUNT_ID
        assert operation == "protocol-capture"
        result = await supervisor.ensure(account_id)
        assert result.state is CredentialResultState.SUCCESS
        assert client.on_state is not None
        assert client.on_error is not None
        assert client.on_auth_failure is not None
        await client.on_state(
            SimpleNamespace(state=SimpleNamespace(value="connected"), detail=None)
        )
        await client.on_error(
            SimpleNamespace(
                code="ws_auth",
                message="TOP-SECRET-STANDALONE-TOKEN cookie=session-secret",
            )
        )
        retry_decisions.append(await client.on_auth_failure(WsAuthError("expired")))

    monkeypatch.setattr(protocol_cli.domain_accounts, "get_account", get_account)
    monkeypatch.setattr(protocol_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(protocol_cli, "_build_credential_supervisor", lambda: supervisor)
    monkeypatch.setattr(protocol_cli, "AccountConnectionLock", lambda _path: object())
    monkeypatch.setattr(protocol_cli, "WsClient", build_client)
    monkeypatch.setattr(protocol_cli, "_start_with_prepared_credentials", start_with_prepare)
    monkeypatch.setattr(
        protocol_cli.console,
        "print",
        lambda value, **_kwargs: output.append(str(value)),
    )

    protocol_cli.capture(
        account_id=ACCOUNT_ID,
        seconds=0.001,
        output=str(tmp_path / "capture.jsonl"),
        target_messages=0,
    )

    assert backend.acquire_calls == [False, True]
    assert retry_decisions == [False]
    assert clients[0].stop_calls == 1
    rendered = "\n".join(output)
    assert "TOP-SECRET-STANDALONE-TOKEN" not in rendered
    assert "cookie=" not in rendered.lower()


@pytest.mark.asyncio
async def test_standalone_auth_recovery_needs_validation_stops_transport_retry() -> None:
    backend = _healthy_standalone_backend()
    supervisor = CredentialSupervisor(backend, _StandaloneValidation(required=True))
    terminal = asyncio.Event()

    stop_retry = await protocol_cli._recover_standalone_auth_failure(
        ACCOUNT_ID,
        supervisor,
        on_terminal=terminal.set,
    )

    assert stop_retry is True
    assert terminal.is_set() is True
    assert backend.acquire_calls == []


@pytest.mark.asyncio
async def test_standalone_auth_recovery_retryable_failure_keeps_transport_policy() -> None:
    backend = _healthy_standalone_backend()
    backend.error = CredentialBackendError(
        CredentialFailureCode.NETWORK_ERROR,
        retryable_hint=True,
    )
    supervisor = CredentialSupervisor(backend, _StandaloneValidation())
    terminal = asyncio.Event()

    stop_retry = await protocol_cli._recover_standalone_auth_failure(
        ACCOUNT_ID,
        supervisor,
        on_terminal=terminal.set,
    )

    assert stop_retry is False
    assert terminal.is_set() is False
    assert backend.acquire_calls == [True]


class _RecoveryResultSupervisor:
    def __init__(self, result: CredentialResult[object]) -> None:
        self.result = result
        self.refresh_calls: list[tuple[str, bool]] = []

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[object]:
        self.refresh_calls.append((account_id, validation_recovery))
        return self.result


@pytest.mark.asyncio
async def test_standalone_auth_recovery_terminal_failure_is_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = _health(CredentialHealthState.HEALTHY)
    supervisor = _RecoveryResultSupervisor(
        CredentialResult(
            state=CredentialResultState.TERMINAL_FAILURE,
            health=health,
            code=CredentialFailureCode.AUTH_FAILED.value,
            message="TOP-SECRET-STANDALONE-TOKEN cookie=session-secret",
        )
    )
    terminal = asyncio.Event()
    output: list[str] = []
    monkeypatch.setattr(
        protocol_cli.console,
        "print",
        lambda value, **_kwargs: output.append(str(value)),
    )

    stop_retry = await protocol_cli._recover_standalone_auth_failure(  # type: ignore[arg-type]
        ACCOUNT_ID,
        supervisor,
        on_terminal=terminal.set,
    )

    assert stop_retry is True
    assert terminal.is_set() is True
    assert supervisor.refresh_calls == [(ACCOUNT_ID, False)]
    rendered = "\n".join(output)
    assert "TOP-SECRET-STANDALONE-TOKEN" not in rendered
    assert "cookie=" not in rendered.lower()


class _ConnectTimerClient:
    def __init__(self) -> None:
        self.started = False
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


def _install_connect_timer_boundary(
    monkeypatch: pytest.MonkeyPatch,
    clients: list[_ConnectTimerClient],
) -> None:
    settings = SimpleNamespace(
        ws_url="wss://unit.test/ws",
        account_lock_path=lambda _account_id: "unit-account.lock",
    )
    supervisor = object()

    def build_client(*_args, **_kwargs):
        client = _ConnectTimerClient()
        clients.append(client)
        return client

    monkeypatch.setattr(protocol_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(protocol_cli, "_build_credential_supervisor", lambda: supervisor)
    monkeypatch.setattr(protocol_cli, "AccountConnectionLock", lambda _path: object())
    monkeypatch.setattr(protocol_cli, "WsClient", build_client)
    monkeypatch.setattr(protocol_cli.console, "print", lambda *_args, **_kwargs: None)


def test_protocol_connect_observation_timer_starts_after_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 0.0}
    monotonic_calls: list[float] = []
    wait_calls: list[float] = []
    clients: list[_ConnectTimerClient] = []
    _install_connect_timer_boundary(monkeypatch, clients)

    async def delayed_prepare(
        account_id,
        client,
        _account_lock,
        *,
        operation,
        supervisor,
    ) -> None:
        assert account_id == ACCOUNT_ID
        assert operation == "protocol-connect"
        assert supervisor is not None
        clock["now"] = 30.0
        client.started = True

    def monotonic() -> float:
        assert clients
        assert clients[0].started is True
        monotonic_calls.append(clock["now"])
        return clock["now"]

    async def wait_for(awaitable, **kwargs):
        awaitable.close()
        timeout = kwargs["timeout"]
        wait_calls.append(timeout)
        clock["now"] = 40.0
        raise TimeoutError

    monkeypatch.setattr(protocol_cli, "_start_with_prepared_credentials", delayed_prepare)
    monkeypatch.setattr(protocol_cli, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(protocol_cli.asyncio, "wait_for", wait_for)

    protocol_cli.connect(account_id=ACCOUNT_ID, seconds=10.0, quiet=True)

    assert monotonic_calls == [30.0, 30.0, 40.0]
    assert wait_calls == [0.5]
    assert clients[0].stop_calls == 1


def test_protocol_connect_preparation_failure_does_not_start_observation_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_calls = 0
    clients: list[_ConnectTimerClient] = []
    _install_connect_timer_boundary(monkeypatch, clients)

    async def failing_prepare(
        _account_id,
        _client,
        _account_lock,
        *,
        operation,
        supervisor,
    ) -> None:
        assert operation == "protocol-connect"
        assert supervisor is not None
        raise typer.Exit(code=2)

    def monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 0.0

    monkeypatch.setattr(protocol_cli, "_start_with_prepared_credentials", failing_prepare)
    monkeypatch.setattr(protocol_cli, "time", SimpleNamespace(monotonic=monotonic))

    with pytest.raises(typer.Exit) as exc_info:
        protocol_cli.connect(account_id=ACCOUNT_ID, seconds=10.0, quiet=True)

    assert exc_info.value.exit_code == 2
    assert monotonic_calls == 0
    assert clients
    assert clients[0].started is False
    assert clients[0].stop_calls == 0


def test_account_connection_lock_handoff_has_no_prepare_start_release_gap(tmp_path) -> None:
    path = tmp_path / "account.lock"
    owner = AccountConnectionLock(path)
    competitor = AccountConnectionLock(path)

    owner.acquire_for_transport_handoff(owner_id="cli:test-preparation")
    with pytest.raises(AccountConnectionAlreadyRunningError):
        competitor.acquire(owner_id="ws:competitor-before-start")

    owner.acquire(owner_id="ws:transport-owner")
    with pytest.raises(AccountConnectionAlreadyRunningError):
        competitor.acquire(owner_id="ws:competitor-after-handoff")

    owner.release()
    competitor.acquire(owner_id="ws:competitor-after-stop")
    competitor.release()


class _WorkerClient:
    def __init__(self) -> None:
        self.state = ConnectionState.IDLE
        self.subscription_ready = None
        self.on_state = None
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


class _ConcurrentValidationCredentials:
    def __init__(self) -> None:
        self.refresh_calls: list[bool] = []
        self.refresh_entered = asyncio.Event()
        self.allow_refresh = asyncio.Event()

    async def ensure(self, account_id: str) -> CredentialResult[object]:
        return CredentialResult(
            state=CredentialResultState.NEEDS_VALIDATION,
            health=CredentialHealth(
                account_id=account_id,
                state=CredentialHealthState.NEEDS_VALIDATION,
            ),
            code=CredentialFailureCode.NEEDS_VALIDATION.value,
        )

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
        defer_validation_clear: bool = False,
    ) -> CredentialResult[object]:
        self.refresh_calls.append(validation_recovery)
        self.refresh_entered.set()
        await self.allow_refresh.wait()
        return CredentialResult(
            state=CredentialResultState.SUCCESS,
            health=CredentialHealth(
                account_id=account_id,
                state=CredentialHealthState.HEALTHY,
            ),
            credential=CredentialHandle("fresh-validation-token"),
            refreshed=True,
        )


@pytest.mark.asyncio
async def test_concurrent_recover_validation_refreshes_and_starts_exactly_once() -> None:
    client = _WorkerClient()
    credentials = _ConcurrentValidationCredentials()
    worker = AccountWorker(
        ACCOUNT_ID,
        client=client,  # type: ignore[arg-type]
        credential_supervisor=credentials,  # type: ignore[arg-type]
        recovery_supervisor=RecoverySupervisor(credentials),  # type: ignore[arg-type]
        persist_events=False,
        automation_mode="passive",
    )
    startup = worker.start()
    assert startup is not None
    await startup
    assert worker.state is WorkerState.NEEDS_VALIDATION
    assert client.start_calls == 0

    first = asyncio.create_task(worker.recover_validation())
    await credentials.refresh_entered.wait()
    second = asyncio.create_task(worker.recover_validation())
    await asyncio.sleep(0)
    assert credentials.refresh_calls == [True]

    credentials.allow_refresh.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert (first_result, second_result) == (True, False)
    assert credentials.refresh_calls == [True]
    assert client.start_calls == 1
    assert worker.state is WorkerState.CONNECTING


class _ReconnectCredentialSource:
    def __init__(self) -> None:
        self.current = "stale-token"
        self.transport_reads: list[str] = []
        self.refresh_calls: list[bool] = []

    async def get_credentials(
        self,
        _account_id: str,
        *,
        force_refresh: bool = False,
    ) -> str:
        assert force_refresh is False, "transport must never request credential refresh"
        self.transport_reads.append(self.current)
        return self.current

    async def ensure(self, account_id: str) -> CredentialResult[object]:
        return CredentialResult(
            state=CredentialResultState.SUCCESS,
            health=CredentialHealth(account_id=account_id, state=CredentialHealthState.HEALTHY),
            credential=CredentialHandle(self.current),
        )

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[object]:
        self.refresh_calls.append(validation_recovery)
        self.current = "fresh-token"
        return CredentialResult(
            state=CredentialResultState.SUCCESS,
            health=CredentialHealth(account_id=account_id, state=CredentialHealthState.HEALTHY),
            credential=CredentialHandle(self.current),
            refreshed=True,
        )


@pytest.mark.asyncio
async def test_auth_failure_refreshes_via_worker_then_transport_retry_reads_new_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _ReconnectCredentialSource()
    client = WsClient(
        ACCOUNT_ID,
        config=ClientConfig(
            ws_url="wss://unit.test/ws",
            auth_retry_delay_s=0.0,
            min_backoff_s=0.001,
            max_backoff_s=0.001,
        ),
        token_provider=source,  # type: ignore[arg-type]
    )
    worker = AccountWorker(
        ACCOUNT_ID,
        client=client,
        credential_supervisor=source,  # type: ignore[arg-type]
        recovery_supervisor=RecoverySupervisor(source),  # type: ignore[arg-type]
        persist_events=False,
        automation_mode="passive",
    )
    worker._worker_state = WorkerState.CONNECTING
    attempts = 0

    async def connect_and_serve() -> None:
        nonlocal attempts
        attempts += 1
        credential = await source.get_credentials(ACCOUNT_ID)
        if attempts == 1:
            assert credential == "stale-token"
            raise WsAuthError("AUTH_FAILED")
        assert credential == "fresh-token"
        client._stop.set()

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(client, "_connect_and_serve", connect_and_serve)
    monkeypatch.setattr(client, "_sleep_or_stop", no_wait)

    await client._run_forever()

    assert source.refresh_calls == [False]
    assert source.transport_reads == ["stale-token", "fresh-token"]
    assert attempts == 2
    assert worker.state is WorkerState.CHECKING_SESSION
