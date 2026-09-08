"""Recovery decision and credential-routing foundation for account workers.

This module intentionally does not own the WebSocket loop, AccountWorker lifecycle, ORM
persistence, or watchdog process management.  It consumes the canonical WorkerState and
CredentialSupervisor result contracts and turns recovery observations into deterministic,
secret-free decisions that later runtime integration can apply.

``RecoveryDecision.worker_state`` is the recovery phase the caller should converge toward.
It is not permission to skip the canonical WorkerState transition graph; AccountWorker
integration remains responsible for sequencing valid intermediate transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Generic, Protocol, TypeVar

from xianyu_agent.application.session.health import CredentialResult, CredentialResultState
from xianyu_agent.application.session.ports import CredentialFailureCode
from xianyu_agent.domain.runtime.worker_state import WorkerState

CredentialT_co = TypeVar("CredentialT_co", covariant=True)
CredentialT = TypeVar("CredentialT")


class RecoveryCause(StrEnum):
    """Stable causes understood by the runtime recovery boundary."""

    TRANSPORT_FAILURE = "transport_failure"
    CREDENTIAL_REQUIRED = "credential_required"
    SESSION_EXPIRED = "session_expired"
    AUTH_FAILURE = "auth_failure"
    CREDENTIAL_TRANSIENT_FAILURE = "credential_transient_failure"
    NEEDS_VALIDATION = "needs_validation"
    TERMINAL_FAILURE = "terminal_failure"
    INTERNAL_FAILURE = "internal_failure"
    STOP_REQUESTED = "stop_requested"
    RECOVERY_SUCCEEDED = "recovery_succeeded"


class RecoveryAction(StrEnum):
    """Action selected by the recovery policy."""

    RETRY_TRANSPORT = "retry_transport"
    ENSURE_CREDENTIAL = "ensure_credential"
    REFRESH_CREDENTIAL = "refresh_credential"
    WAIT_FOR_VALIDATION = "wait_for_validation"
    FAIL = "fail"
    STOP = "stop"
    RESUME = "resume"


class CredentialRecoveryRoute(StrEnum):
    """Explicit CredentialSupervisor operation selected by the caller/policy."""

    NONE = "none"
    ENSURE = "ensure"
    REFRESH = "refresh"
    VALIDATION_REFRESH = "validation_refresh"


class RecoveryConfigurationError(RuntimeError):
    """Raised when execution needs a dependency that was not configured."""


class CredentialRecoveryPort(Protocol[CredentialT_co]):
    """Structural subset of CredentialSupervisor used by recovery."""

    async def ensure(self, account_id: str) -> CredentialResult[CredentialT_co]: ...

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[CredentialT_co]: ...


@dataclass(frozen=True)
class RecoveryBackoffPolicy:
    """Bounded exponential backoff with caller-supplied deterministic jitter.

    ``attempt`` is one-based and mirrors the legacy reconnect loop: attempt 1 starts at
    ``min_delay * multiplier``.  Jitter is bounded and the final returned delay never
    exceeds ``max_delay_s``.
    """

    min_delay_s: float = 1.0
    max_delay_s: float = 60.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.5

    def __post_init__(self) -> None:
        if self.min_delay_s <= 0:
            raise ValueError("min_delay_s must be positive")
        if self.max_delay_s < self.min_delay_s:
            raise ValueError("max_delay_s must be >= min_delay_s")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay_for(self, attempt: int, *, jitter_unit: float = 0.0) -> float:
        """Return a bounded delay for a one-based retry attempt."""

        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        if not 0 <= jitter_unit <= 1:
            raise ValueError("jitter_unit must be between 0 and 1")

        exponent = min(attempt, 32)
        base = min(
            self.max_delay_s,
            self.min_delay_s * (self.multiplier**exponent),
        )
        jitter = base * self.jitter_ratio * jitter_unit
        return min(self.max_delay_s, base + jitter)


@dataclass(frozen=True)
class RecoveryDecision:
    """Secret-free recovery decision safe for diagnostics and persistence adapters."""

    cause: RecoveryCause
    action: RecoveryAction
    worker_state: WorkerState
    credential_route: CredentialRecoveryRoute = CredentialRecoveryRoute.NONE
    retry: bool = False
    retry_delay_s: float | None = None
    code: str | None = None


@dataclass(frozen=True)
class RecoveryOutcome(Generic[CredentialT]):
    """Decision plus the CredentialSupervisor result, hidden from routine repr output."""

    decision: RecoveryDecision
    credential_result: CredentialResult[CredentialT] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def recovery_cause_for_credential_code(code: str | CredentialFailureCode | None) -> RecoveryCause:
    """Normalize a stable credential failure code into a recovery cause."""

    if code is None:
        failure = None
    else:
        raw = code.value if isinstance(code, CredentialFailureCode) else str(code).strip().upper()
        try:
            failure = CredentialFailureCode(raw)
        except ValueError:
            failure = None

    special_causes = {
        CredentialFailureCode.SESSION_EXPIRED: RecoveryCause.SESSION_EXPIRED,
        CredentialFailureCode.AUTH_FAILED: RecoveryCause.AUTH_FAILURE,
        CredentialFailureCode.NETWORK_ERROR: RecoveryCause.CREDENTIAL_TRANSIENT_FAILURE,
        CredentialFailureCode.NEEDS_VALIDATION: RecoveryCause.NEEDS_VALIDATION,
        CredentialFailureCode.INTERNAL_ERROR: RecoveryCause.INTERNAL_FAILURE,
    }
    if failure is None:
        return RecoveryCause.INTERNAL_FAILURE
    return special_causes.get(failure, RecoveryCause.TERMINAL_FAILURE)


class RecoverySupervisor(Generic[CredentialT]):
    """Classify recovery causes and route explicit credential operations.

    Validation recovery is never inferred from a failure.  ``VALIDATION_REFRESH`` can only
    be supplied explicitly by a caller that is handling an authorized manual recovery
    action; an observed NEEDS_VALIDATION result always stops automatic retry.
    """

    def __init__(
        self,
        credentials: CredentialRecoveryPort[CredentialT] | None = None,
        *,
        backoff: RecoveryBackoffPolicy | None = None,
    ) -> None:
        self._credentials = credentials
        self._backoff = backoff or RecoveryBackoffPolicy()

    def decide(
        self,
        cause: RecoveryCause,
        *,
        credential_route: CredentialRecoveryRoute = CredentialRecoveryRoute.NONE,
        attempt: int = 1,
        jitter_unit: float = 0.0,
        code: str | None = None,
    ) -> RecoveryDecision:
        """Return the deterministic policy decision for one recovery observation."""

        if cause is RecoveryCause.TRANSPORT_FAILURE:
            decision = self._retry_decision(
                cause,
                action=RecoveryAction.RETRY_TRANSPORT,
                worker_state=WorkerState.RECONNECTING,
                route=CredentialRecoveryRoute.NONE,
                attempt=attempt,
                jitter_unit=jitter_unit,
                code=code,
            )
        elif cause is RecoveryCause.CREDENTIAL_REQUIRED:
            decision = RecoveryDecision(
                cause=cause,
                action=RecoveryAction.ENSURE_CREDENTIAL,
                worker_state=WorkerState.CHECKING_SESSION,
                credential_route=CredentialRecoveryRoute.ENSURE,
                code=code,
            )
        elif cause in {RecoveryCause.SESSION_EXPIRED, RecoveryCause.AUTH_FAILURE}:
            decision = RecoveryDecision(
                cause=cause,
                action=RecoveryAction.REFRESH_CREDENTIAL,
                worker_state=WorkerState.REFRESHING_CREDENTIAL,
                credential_route=CredentialRecoveryRoute.REFRESH,
                code=code,
            )
        elif cause is RecoveryCause.CREDENTIAL_TRANSIENT_FAILURE:
            if credential_route is CredentialRecoveryRoute.NONE:
                raise ValueError("credential transient failure requires an explicit route")
            decision = self._retry_decision(
                cause,
                action=self._action_for_route(credential_route),
                worker_state=self._retry_state_for_route(credential_route),
                route=credential_route,
                attempt=attempt,
                jitter_unit=jitter_unit,
                code=code,
            )
        elif cause is RecoveryCause.NEEDS_VALIDATION:
            decision = RecoveryDecision(
                cause=cause,
                action=RecoveryAction.WAIT_FOR_VALIDATION,
                worker_state=WorkerState.NEEDS_VALIDATION,
                code=code or CredentialFailureCode.NEEDS_VALIDATION.value,
            )
        elif cause in {RecoveryCause.TERMINAL_FAILURE, RecoveryCause.INTERNAL_FAILURE}:
            decision = RecoveryDecision(
                cause=cause,
                action=RecoveryAction.FAIL,
                worker_state=WorkerState.ERROR,
                code=code,
            )
        elif cause is RecoveryCause.STOP_REQUESTED:
            decision = RecoveryDecision(
                cause=cause,
                action=RecoveryAction.STOP,
                worker_state=WorkerState.STOPPING,
                code=code,
            )
        elif cause is RecoveryCause.RECOVERY_SUCCEEDED:
            if credential_route is CredentialRecoveryRoute.NONE:
                raise ValueError("successful recovery requires the completed credential route")
            decision = RecoveryDecision(
                cause=cause,
                action=RecoveryAction.RESUME,
                worker_state=self._success_state_for_route(credential_route),
                code=code,
            )
        else:
            raise AssertionError(f"unhandled recovery cause: {cause!r}")
        return decision

    def decide_for_credential_result(
        self,
        result: CredentialResult[CredentialT],
        *,
        route: CredentialRecoveryRoute,
        attempt: int = 1,
        jitter_unit: float = 0.0,
    ) -> RecoveryDecision:
        """Translate a CredentialSupervisor result without exposing credential material."""

        if route is CredentialRecoveryRoute.NONE:
            raise ValueError("credential result requires an explicit route")

        if result.state is CredentialResultState.SUCCESS:
            return self.decide(
                RecoveryCause.RECOVERY_SUCCEEDED,
                credential_route=route,
                code=result.code,
            )
        if result.state is CredentialResultState.RETRYABLE_FAILURE:
            return self.decide(
                RecoveryCause.CREDENTIAL_TRANSIENT_FAILURE,
                credential_route=route,
                attempt=attempt,
                jitter_unit=jitter_unit,
                code=result.code,
            )
        if result.state is CredentialResultState.NEEDS_VALIDATION:
            return self.decide(
                RecoveryCause.NEEDS_VALIDATION,
                code=result.code,
            )

        cause = (
            RecoveryCause.INTERNAL_FAILURE
            if recovery_cause_for_credential_code(result.code) is RecoveryCause.INTERNAL_FAILURE
            else RecoveryCause.TERMINAL_FAILURE
        )
        return self.decide(cause, code=result.code)

    async def recover_credential(
        self,
        account_id: str,
        *,
        route: CredentialRecoveryRoute,
        attempt: int = 1,
        jitter_unit: float = 0.0,
    ) -> RecoveryOutcome[CredentialT]:
        """Execute one explicit credential route and classify its result."""

        if route is CredentialRecoveryRoute.NONE:
            raise ValueError("credential recovery execution requires an explicit route")
        if self._credentials is None:
            raise RecoveryConfigurationError("credential recovery port is not configured")

        try:
            if route is CredentialRecoveryRoute.ENSURE:
                result = await self._credentials.ensure(account_id)
            elif route is CredentialRecoveryRoute.REFRESH:
                result = await self._credentials.refresh(
                    account_id,
                    validation_recovery=False,
                )
            else:
                result = await self._credentials.refresh(
                    account_id,
                    validation_recovery=True,
                )
        except Exception:
            decision = self.decide(
                RecoveryCause.INTERNAL_FAILURE,
                code=CredentialFailureCode.INTERNAL_ERROR.value,
            )
            return RecoveryOutcome(decision=decision)

        decision = self.decide_for_credential_result(
            result,
            route=route,
            attempt=attempt,
            jitter_unit=jitter_unit,
        )
        return RecoveryOutcome(decision=decision, credential_result=result)

    def _retry_decision(
        self,
        cause: RecoveryCause,
        *,
        action: RecoveryAction,
        worker_state: WorkerState,
        route: CredentialRecoveryRoute,
        attempt: int,
        jitter_unit: float,
        code: str | None,
    ) -> RecoveryDecision:
        return RecoveryDecision(
            cause=cause,
            action=action,
            worker_state=worker_state,
            credential_route=route,
            retry=True,
            retry_delay_s=self._backoff.delay_for(
                attempt,
                jitter_unit=jitter_unit,
            ),
            code=code,
        )

    @staticmethod
    def _action_for_route(route: CredentialRecoveryRoute) -> RecoveryAction:
        if route is CredentialRecoveryRoute.ENSURE:
            return RecoveryAction.ENSURE_CREDENTIAL
        return RecoveryAction.REFRESH_CREDENTIAL

    @staticmethod
    def _retry_state_for_route(route: CredentialRecoveryRoute) -> WorkerState:
        if route is CredentialRecoveryRoute.ENSURE:
            return WorkerState.CHECKING_SESSION
        if route is CredentialRecoveryRoute.VALIDATION_REFRESH:
            return WorkerState.NEEDS_VALIDATION
        return WorkerState.REFRESHING_CREDENTIAL

    @staticmethod
    def _success_state_for_route(route: CredentialRecoveryRoute) -> WorkerState:
        if route is CredentialRecoveryRoute.ENSURE:
            return WorkerState.CONNECTING
        return WorkerState.CHECKING_SESSION
