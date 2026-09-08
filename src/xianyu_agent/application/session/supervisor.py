"""Application-layer orchestration for credential lifecycle operations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
                clear_validation=False,
            )

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[CredentialT]:
        """Force refresh without implicitly bypassing NEEDS_VALIDATION.

        A validation recovery attempt must be explicit and is rejected while the
        persisted cooldown is active. Successful explicit recovery clears the legacy
        validation gate through its port.
        """
        async with self._lock_for(account_id):
            health = await self._safe_inspect(account_id)
            if health.state is CredentialHealthState.ERROR:
                return self._terminal_internal(health)
            recovering_validation = health.state is CredentialHealthState.NEEDS_VALIDATION
            if recovering_validation and (
                not validation_recovery or health.validation_cooling
            ):
                return self._needs_validation(health)
            if health.state in {
                CredentialHealthState.MISSING,
                CredentialHealthState.UNUSABLE,
            }:
                return self._blocked_result(health) or self._terminal_internal(health)
            return await self._acquire(
                account_id,
                health,
                force_refresh=True,
                clear_validation=recovering_validation,
            )

    async def _acquire(
        self,
        account_id: str,
        prior_health: CredentialHealth,
        *,
        force_refresh: bool,
        clear_validation: bool,
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

        if clear_validation:
            try:
                await self._validation.clear_after_refresh(account_id)
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
