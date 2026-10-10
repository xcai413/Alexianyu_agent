"""Application-layer orchestration for credential lifecycle operations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Generic, TypeVar

from .health import (
    CredentialHandle,
    CredentialHealth,
    CredentialHealthState,
    CredentialResult,
    CredentialResultState,
)
from .policy import CredentialPolicy
from .ports import (
    CredentialBackend,
    CredentialBackendError,
    CredentialFailureCode,
    ValidationGate,
)

CredentialT = TypeVar("CredentialT")
Clock = Callable[[], datetime]


class CredentialSupervisor(Generic[CredentialT]):
    """Coordinate health checks, refresh policy, and validation gates.

    This class owns no WebSocket transport or Worker state. Per-account locks make
    refresh decisions idempotent within a process and are always released by the async
    context manager, including exception paths.
    """

    def __init__(
        self,
        backend: CredentialBackend[CredentialT],
        validation: ValidationGate,
        *,
        policy: CredentialPolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._backend = backend
        self._validation = validation
        self._policy = policy or CredentialPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}
        self._prepared_revisions: dict[str, str | None] = {}

    async def inspect(self, account_id: str) -> CredentialHealth:
        """Return a secret-free health summary."""
        status = await self._backend.inspect(account_id)
        validation = await self._validation.inspect(account_id)
        return self._policy.health(status, validation, now=self._clock())

    async def ensure(self, account_id: str) -> CredentialResult[CredentialT]:
        """Return usable credential material, refreshing only when policy requires it."""
        async with self._lock_for(account_id):
            health = await self._safe_inspect(account_id)
            blocked = self._blocked_result(health)
            if blocked is not None:
                return blocked
            return await self._acquire(
                account_id,
                health,
                force_refresh=health.refresh_recommended,
            )

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
        defer_validation_clear: bool = True,
    ) -> CredentialResult[CredentialT]:
        """Force refresh without implicitly bypassing NEEDS_VALIDATION.

        A validation recovery attempt must be explicit and is rejected while the
        persisted cooldown is active. A deferred recovery returns prepared credentials
        while keeping the validation gate closed to automation; the live-session
        owner must call confirm_recovered after subscription readiness.
        """
        async with self._lock_for(account_id):
            self._prepared_revisions.pop(account_id, None)
            health = await self._safe_inspect(account_id)
            if health.state is CredentialHealthState.ERROR:
                return self._terminal_internal(health)
            recovering_validation = health.state is CredentialHealthState.NEEDS_VALIDATION
            if recovering_validation and (not validation_recovery or health.validation_cooling):
                return self._needs_validation(health)
            if health.state in {
                CredentialHealthState.MISSING,
                CredentialHealthState.UNUSABLE,
            }:
                blocked = self._blocked_result(health)
                if blocked is not None:
                    return blocked
                return self._terminal_internal(health)
            result = await self._acquire(
                account_id,
                health,
                force_refresh=True,
                defer_validation_clear=recovering_validation or defer_validation_clear,
            )
            if recovering_validation and result.ok:
                self._prepared_revisions[account_id] = health.validation_revision
                result = replace(result, validation_revision=health.validation_revision)
            return result

    async def confirm_recovered(
        self, account_id: str, *, expected_revision: str | None = None
    ) -> bool:
        """Clear a validation gate only after the caller verified the live session."""
        async with self._lock_for(account_id):
            if expected_revision is None:
                return False
            if account_id not in self._prepared_revisions:
                return False
            revision = self._prepared_revisions[account_id]
            if expected_revision is not None and expected_revision != revision:
                return False
            self._prepared_revisions.pop(account_id)
            if revision is None:
                return False
            return await self._validation.clear_after_refresh(
                account_id, expected_revision=revision
            )

    async def _acquire(
        self,
        account_id: str,
        prior_health: CredentialHealth,
        *,
        force_refresh: bool,
        defer_validation_clear: bool = False,
    ) -> CredentialResult[CredentialT]:
        try:
            material = await self._backend.acquire(
                account_id,
                force_refresh=force_refresh,
            )
        except CredentialBackendError as exc:
            return await self._backend_failure(account_id, prior_health, exc)
        except Exception:
            return self._terminal_internal(
                CredentialHealth(
                    account_id=account_id,
                    state=CredentialHealthState.ERROR,
                    code=CredentialFailureCode.INTERNAL_ERROR.value,
                )
            )

        health = await self._safe_inspect(account_id)
        if health.state is CredentialHealthState.ERROR:
            return self._terminal_internal(health)
        if health.state is CredentialHealthState.NEEDS_VALIDATION and not defer_validation_clear:
            return self._needs_validation(health)
        return CredentialResult(
            state=CredentialResultState.SUCCESS,
            health=health,
            credential=CredentialHandle(material),
            refreshed=force_refresh,
        )

    async def _backend_failure(
        self,
        account_id: str,
        prior_health: CredentialHealth,
        error: CredentialBackendError,
    ) -> CredentialResult[CredentialT]:
        state = self._policy.result_state_for(error)
        health = prior_health
        if state is CredentialResultState.NEEDS_VALIDATION:
            try:
                await self._validation.mark_needs_validation(account_id)
                health = await self._safe_inspect(account_id)
            except Exception:
                return self._terminal_internal(
                    CredentialHealth(
                        account_id=account_id,
                        state=CredentialHealthState.ERROR,
                        code=CredentialFailureCode.INTERNAL_ERROR.value,
                    )
                )
        return CredentialResult(
            state=state,
            health=health,
            code=error.code.value,
            message=self._policy.safe_message(error.code),
        )

    async def _safe_inspect(self, account_id: str) -> CredentialHealth:
        try:
            return await self.inspect(account_id)
        except Exception:
            return CredentialHealth(
                account_id=account_id,
                state=CredentialHealthState.ERROR,
                code=CredentialFailureCode.INTERNAL_ERROR.value,
            )

    def _blocked_result(
        self,
        health: CredentialHealth,
    ) -> CredentialResult[CredentialT] | None:
        if health.state is CredentialHealthState.NEEDS_VALIDATION:
            return self._needs_validation(health)
        if health.state is CredentialHealthState.MISSING:
            code = CredentialFailureCode.CREDENTIAL_MISSING
        elif health.state is CredentialHealthState.UNUSABLE:
            code = CredentialFailureCode.IDENTITY_MISSING
        elif health.state is CredentialHealthState.ERROR:
            return self._terminal_internal(health)
        else:
            return None
        return CredentialResult(
            state=CredentialResultState.TERMINAL_FAILURE,
            health=health,
            code=code.value,
            message=self._policy.safe_message(code),
        )

    def _needs_validation(self, health: CredentialHealth) -> CredentialResult[CredentialT]:
        code = CredentialFailureCode.NEEDS_VALIDATION
        return CredentialResult(
            state=CredentialResultState.NEEDS_VALIDATION,
            health=health,
            code=code.value,
            message=self._policy.safe_message(code),
        )

    def _terminal_internal(self, health: CredentialHealth) -> CredentialResult[CredentialT]:
        code = CredentialFailureCode.INTERNAL_ERROR
        return CredentialResult(
            state=CredentialResultState.TERMINAL_FAILURE,
            health=health,
            code=code.value,
            message=self._policy.safe_message(code),
        )

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        lock = self._locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account_id] = lock
        return lock
