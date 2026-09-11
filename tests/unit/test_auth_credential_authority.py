"""Regression coverage for canonical credential mutation authority."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import typer

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
from xianyu_agent.cli.commands import auth as auth_cli
from xianyu_agent.cli.commands import order as order_cli
from xianyu_agent.cli.commands import protocol as protocol_cli
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

    auth_cli.refresh(account_id=ACCOUNT_ID)

    assert supervisor.inspect_calls == [ACCOUNT_ID]
    assert supervisor.refresh_calls == [(ACCOUNT_ID, True)]
    assert lock.release_calls == 1
    assert held["value"] is False


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
    monkeypatch.setattr(auth_cli.console, "print", lambda value, **_kwargs: output.append(str(value)))

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
        )

    async def mark_needs_validation(self, account_id: str) -> None:
        assert account_id == ACCOUNT_ID
        self.mark_calls += 1
        self.required = True

    async def clear_after_refresh(self, account_id: str) -> bool:
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
