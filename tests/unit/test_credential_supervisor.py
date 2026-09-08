from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from xianyu_agent.application.session import (
    CredentialBackendError,
    CredentialBackendStatus,
    CredentialFailureCode,
    CredentialHealthState,
    CredentialResultState,
    CredentialSupervisor,
    ValidationStatus,
)

NOW = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
ACCOUNT_ID = "account-1"


@dataclass(frozen=True)
class SecretCredential:
    access_token: str


class FakeBackend:
    def __init__(self, status: CredentialBackendStatus) -> None:
        self.status = status
        self.material = SecretCredential("super-secret-access-token")
        self.acquire_calls: list[bool] = []
        self.failures: list[BaseException] = []

    async def inspect(self, account_id: str) -> CredentialBackendStatus:
        assert account_id == ACCOUNT_ID
        return self.status

    async def acquire(
        self,
        account_id: str,
        *,
        force_refresh: bool = False,
    ) -> SecretCredential:
        assert account_id == ACCOUNT_ID
        self.acquire_calls.append(force_refresh)
        if self.failures:
            raise self.failures.pop(0)
        if force_refresh:
            self.status = replace(
                self.status,
                token_cached=True,
                expires_at=NOW + timedelta(hours=8),
            )
        return self.material


class FakeValidationGate:
    def __init__(
        self,
        status: ValidationStatus | None = None,
        *,
        clear_succeeds: bool = True,
    ) -> None:
        self.status = status or ValidationStatus(account_id=ACCOUNT_ID, required=False)
        self.clear_succeeds = clear_succeeds
        self.mark_calls = 0
        self.clear_calls = 0

    async def inspect(self, account_id: str) -> ValidationStatus:
        assert account_id == ACCOUNT_ID
        return self.status

    async def mark_needs_validation(self, account_id: str) -> None:
        assert account_id == ACCOUNT_ID
        self.mark_calls += 1
        self.status = ValidationStatus(
            account_id=ACCOUNT_ID,
            required=True,
            cooling=True,
            code=CredentialFailureCode.NEEDS_VALIDATION.value,
        )

    async def clear_after_refresh(self, account_id: str) -> bool:
        assert account_id == ACCOUNT_ID
        self.clear_calls += 1
        if not self.clear_succeeds:
            return False
        was_required = self.status.required
        self.status = ValidationStatus(account_id=ACCOUNT_ID, required=False)
        return was_required


def _status(
    *,
    cookie: bool = True,
    identity: bool = True,
    token_cached: bool = True,
    expires_at: datetime | None = None,
) -> CredentialBackendStatus:
    return CredentialBackendStatus(
        account_id=ACCOUNT_ID,
        cookie_available=cookie,
        identity_available=identity,
        token_cached=token_cached,
        expires_at=expires_at if expires_at is not None else NOW + timedelta(hours=1),
    )


def _supervisor(
    backend: FakeBackend,
    validation: FakeValidationGate | None = None,
) -> CredentialSupervisor[SecretCredential]:
    return CredentialSupervisor(
        backend,
        validation or FakeValidationGate(),
        clock=lambda: NOW,
    )


async def test_healthy_credential_reuses_cached_material() -> None:
    backend = FakeBackend(_status())
    result = await _supervisor(backend).ensure(ACCOUNT_ID)

    assert result.state is CredentialResultState.SUCCESS
    assert result.health.state is CredentialHealthState.HEALTHY
    assert result.refreshed is False
    assert backend.acquire_calls == [False]
    assert result.credential is not None
    assert result.credential.unwrap() is backend.material


async def test_missing_credential_is_terminal_without_refresh_attempt() -> None:
    backend = FakeBackend(_status(cookie=False, identity=False, token_cached=False))
    result = await _supervisor(backend).ensure(ACCOUNT_ID)

    assert result.state is CredentialResultState.TERMINAL_FAILURE
    assert result.health.state is CredentialHealthState.MISSING
    assert result.code == CredentialFailureCode.CREDENTIAL_MISSING.value
    assert backend.acquire_calls == []


async def test_refresh_required_performs_forced_refresh() -> None:
    backend = FakeBackend(_status(token_cached=False))
    backend.status = replace(backend.status, expires_at=None)
    result = await _supervisor(backend).ensure(ACCOUNT_ID)

    assert result.state is CredentialResultState.SUCCESS
    assert result.health.state is CredentialHealthState.HEALTHY
    assert result.refreshed is True
    assert backend.acquire_calls == [True]


async def test_retryable_refresh_failure_is_classified() -> None:
    backend = FakeBackend(_status(token_cached=False))
    backend.failures.append(
        CredentialBackendError(
            CredentialFailureCode.NETWORK_ERROR,
            retryable_hint=True,
        )
    )

    result = await _supervisor(backend).ensure(ACCOUNT_ID)

    assert result.state is CredentialResultState.RETRYABLE_FAILURE
    assert result.code == CredentialFailureCode.NETWORK_ERROR.value
    assert result.message == "Credential refresh failed because of a network error."


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        (NOW - timedelta(seconds=1), CredentialHealthState.EXPIRED),
        (NOW + timedelta(minutes=4), CredentialHealthState.EXPIRING),
    ],
)
async def test_expired_and_early_expiration_force_refresh(
    expires_at: datetime,
    expected: CredentialHealthState,
) -> None:
    backend = FakeBackend(_status(expires_at=expires_at))
    supervisor = _supervisor(backend)

    health = await supervisor.inspect(ACCOUNT_ID)
    assert health.state is expected

    result = await supervisor.ensure(ACCOUNT_ID)
    assert result.state is CredentialResultState.SUCCESS
    assert result.refreshed is True
    assert backend.acquire_calls == [True]


async def test_needs_validation_blocks_automatic_refresh() -> None:
    backend = FakeBackend(_status(token_cached=False))
    validation = FakeValidationGate(
        ValidationStatus(
            account_id=ACCOUNT_ID,
            required=True,
            cooling=False,
            code=CredentialFailureCode.NEEDS_VALIDATION.value,
        )
    )
    result = await _supervisor(backend, validation).ensure(ACCOUNT_ID)

    assert result.state is CredentialResultState.NEEDS_VALIDATION
    assert result.health.state is CredentialHealthState.NEEDS_VALIDATION
    assert backend.acquire_calls == []


async def test_backend_validation_failure_opens_validation_gate() -> None:
    backend = FakeBackend(_status(token_cached=False))
    backend.failures.append(CredentialBackendError(CredentialFailureCode.NEEDS_VALIDATION))
    validation = FakeValidationGate()

    result = await _supervisor(backend, validation).ensure(ACCOUNT_ID)

    assert result.state is CredentialResultState.NEEDS_VALIDATION
    assert result.health.state is CredentialHealthState.NEEDS_VALIDATION
    assert validation.mark_calls == 1


async def test_concurrent_repeated_ensure_refreshes_once() -> None:
    backend = FakeBackend(_status(token_cached=False))
    supervisor = _supervisor(backend)

    first, second = await asyncio.gather(
        supervisor.ensure(ACCOUNT_ID),
        supervisor.ensure(ACCOUNT_ID),
    )

    assert first.ok
    assert second.ok
    assert backend.acquire_calls.count(True) == 1
    assert backend.acquire_calls.count(False) == 1


async def test_exception_path_releases_refresh_lock() -> None:
    backend = FakeBackend(_status(token_cached=False))
    backend.failures.append(RuntimeError("raw backend failure with secret=should-not-leak"))
    supervisor = _supervisor(backend)

    first = await supervisor.ensure(ACCOUNT_ID)
    second = await asyncio.wait_for(supervisor.ensure(ACCOUNT_ID), timeout=1)

    assert first.state is CredentialResultState.TERMINAL_FAILURE
    assert first.code == CredentialFailureCode.INTERNAL_ERROR.value
    assert "should-not-leak" not in repr(first)
    assert second.state is CredentialResultState.SUCCESS


async def test_validation_recovery_requires_cooldown_to_finish_and_clears_gate() -> None:
    backend = FakeBackend(_status(token_cached=False))
    validation = FakeValidationGate(
        ValidationStatus(
            account_id=ACCOUNT_ID,
            required=True,
            cooling=True,
            code=CredentialFailureCode.NEEDS_VALIDATION.value,
        )
    )
    supervisor = _supervisor(backend, validation)

    cooling = await supervisor.refresh(ACCOUNT_ID, validation_recovery=True)
    assert cooling.state is CredentialResultState.NEEDS_VALIDATION
    assert backend.acquire_calls == []

    validation.status = replace(validation.status, cooling=False)
    recovered = await supervisor.refresh(ACCOUNT_ID, validation_recovery=True)

    assert recovered.state is CredentialResultState.SUCCESS
    assert recovered.health.state is CredentialHealthState.HEALTHY
    assert validation.clear_calls == 1
    assert backend.acquire_calls == [True]


async def test_validation_recovery_stays_blocked_when_gate_does_not_clear() -> None:
    backend = FakeBackend(_status(token_cached=False))
    validation = FakeValidationGate(
        ValidationStatus(
            account_id=ACCOUNT_ID,
            required=True,
            cooling=False,
            code=CredentialFailureCode.NEEDS_VALIDATION.value,
        ),
        clear_succeeds=False,
    )
    supervisor = _supervisor(backend, validation)

    result = await supervisor.refresh(ACCOUNT_ID, validation_recovery=True)

    assert result.state is CredentialResultState.NEEDS_VALIDATION
    assert result.health.state is CredentialHealthState.NEEDS_VALIDATION
    assert validation.clear_calls == 1
    assert backend.acquire_calls == [True]


async def test_result_repr_never_contains_credential_material() -> None:
    backend = FakeBackend(_status())
    result = await _supervisor(backend).ensure(ACCOUNT_ID)

    assert "super-secret-access-token" not in repr(result)
    assert result.credential is not None
    assert "super-secret-access-token" not in repr(result.credential)
