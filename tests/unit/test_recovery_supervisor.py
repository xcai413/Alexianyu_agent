from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from xianyu_agent.application.session.health import (
    CredentialHandle,
    CredentialHealth,
    CredentialHealthState,
    CredentialResult,
    CredentialResultState,
)
from xianyu_agent.application.session.ports import CredentialFailureCode
from xianyu_agent.domain.runtime.worker_state import WorkerState
from xianyu_agent.runtime.recovery import (
    CredentialRecoveryRoute,
    RecoveryAction,
    RecoveryBackoffPolicy,
    RecoveryCause,
    RecoveryConfigurationError,
    RecoverySupervisor,
    recovery_cause_for_credential_code,
)


def _health(
    state: CredentialHealthState = CredentialHealthState.HEALTHY,
) -> CredentialHealth:
    return CredentialHealth(account_id="acct-1", state=state)


def _result(
    state: CredentialResultState,
    *,
    code: CredentialFailureCode | None = None,
    secret: str | None = None,
) -> CredentialResult[str]:
    return CredentialResult(
        state=state,
        health=_health(
            CredentialHealthState.NEEDS_VALIDATION
            if state is CredentialResultState.NEEDS_VALIDATION
            else CredentialHealthState.HEALTHY
        ),
        credential=CredentialHandle(secret) if secret is not None else None,
        code=code.value if code is not None else None,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_delay_s": 0},
        {"min_delay_s": 2, "max_delay_s": 1},
        {"multiplier": 0.5},
        {"jitter_ratio": -0.1},
        {"jitter_ratio": 1.1},
    ],
)
def test_backoff_policy_rejects_invalid_configuration(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RecoveryBackoffPolicy(**kwargs)


def test_backoff_matches_legacy_first_attempt_and_grows_exponentially() -> None:
    policy = RecoveryBackoffPolicy()

    assert policy.delay_for(1) == 2.0
    assert policy.delay_for(2) == 4.0
    assert policy.delay_for(5) == 32.0


def test_backoff_jitter_and_final_delay_are_bounded() -> None:
    policy = RecoveryBackoffPolicy()

    assert policy.delay_for(1, jitter_unit=1.0) == 3.0
    assert policy.delay_for(6, jitter_unit=1.0) == 60.0
    assert policy.delay_for(1000, jitter_unit=1.0) == 60.0


@pytest.mark.parametrize(
    ("attempt", "jitter"),
    [
        (0, 0.0),
        (-1, 0.0),
        (1, -0.01),
        (1, 1.01),
    ],
)
def test_backoff_rejects_invalid_inputs(attempt: int, jitter: float) -> None:
    with pytest.raises(ValueError):
        RecoveryBackoffPolicy().delay_for(attempt, jitter_unit=jitter)


def test_transport_failure_retries_in_reconnecting_state() -> None:
    decision = RecoverySupervisor[object]().decide(
        RecoveryCause.TRANSPORT_FAILURE,
        attempt=2,
        jitter_unit=0.5,
        code="socket_closed",
    )

    assert decision.action is RecoveryAction.RETRY_TRANSPORT
    assert decision.worker_state is WorkerState.RECONNECTING
    assert decision.credential_route is CredentialRecoveryRoute.NONE
    assert decision.retry
    assert decision.retry_delay_s == 5.0
    assert decision.code == "socket_closed"


def test_missing_credential_routes_to_ensure() -> None:
    decision = RecoverySupervisor[object]().decide(RecoveryCause.CREDENTIAL_REQUIRED)

    assert decision.action is RecoveryAction.ENSURE_CREDENTIAL
    assert decision.worker_state is WorkerState.CHECKING_SESSION
    assert decision.credential_route is CredentialRecoveryRoute.ENSURE
    assert not decision.retry


@pytest.mark.parametrize(
    "cause",
    [RecoveryCause.SESSION_EXPIRED, RecoveryCause.AUTH_FAILURE],
)
def test_session_and_auth_failures_route_to_refresh(cause: RecoveryCause) -> None:
    decision = RecoverySupervisor[object]().decide(cause)

    assert decision.action is RecoveryAction.REFRESH_CREDENTIAL
    assert decision.worker_state is WorkerState.REFRESHING_CREDENTIAL
    assert decision.credential_route is CredentialRecoveryRoute.REFRESH
    assert not decision.retry


def test_validation_failure_stops_automatic_retry() -> None:
    decision = RecoverySupervisor[object]().decide(RecoveryCause.NEEDS_VALIDATION)

    assert decision.action is RecoveryAction.WAIT_FOR_VALIDATION
    assert decision.worker_state is WorkerState.NEEDS_VALIDATION
    assert decision.credential_route is CredentialRecoveryRoute.NONE
    assert not decision.retry
    assert decision.retry_delay_s is None


@pytest.mark.parametrize(
    "cause",
    [RecoveryCause.TERMINAL_FAILURE, RecoveryCause.INTERNAL_FAILURE],
)
def test_nonrecoverable_failures_converge_to_error(cause: RecoveryCause) -> None:
    decision = RecoverySupervisor[object]().decide(cause)

    assert decision.action is RecoveryAction.FAIL
    assert decision.worker_state is WorkerState.ERROR
    assert not decision.retry


def test_stop_request_converges_to_stopping() -> None:
    decision = RecoverySupervisor[object]().decide(RecoveryCause.STOP_REQUESTED)

    assert decision.action is RecoveryAction.STOP
    assert decision.worker_state is WorkerState.STOPPING
    assert not decision.retry


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (CredentialFailureCode.SESSION_EXPIRED, RecoveryCause.SESSION_EXPIRED),
        (CredentialFailureCode.AUTH_FAILED, RecoveryCause.AUTH_FAILURE),
        (CredentialFailureCode.NETWORK_ERROR, RecoveryCause.CREDENTIAL_TRANSIENT_FAILURE),
        (CredentialFailureCode.NEEDS_VALIDATION, RecoveryCause.NEEDS_VALIDATION),
        (CredentialFailureCode.CREDENTIAL_MISSING, RecoveryCause.TERMINAL_FAILURE),
        (CredentialFailureCode.IDENTITY_MISSING, RecoveryCause.TERMINAL_FAILURE),
        (CredentialFailureCode.ACCOUNT_NOT_FOUND, RecoveryCause.TERMINAL_FAILURE),
        (CredentialFailureCode.INTERNAL_ERROR, RecoveryCause.INTERNAL_FAILURE),
    ],
)
def test_credential_failure_code_classification(
    code: CredentialFailureCode,
    expected: RecoveryCause,
) -> None:
    assert recovery_cause_for_credential_code(code) is expected
    assert recovery_cause_for_credential_code(code.value.lower()) is expected


def test_unknown_credential_failure_code_fails_closed() -> None:
    assert recovery_cause_for_credential_code(None) is RecoveryCause.INTERNAL_FAILURE
    assert recovery_cause_for_credential_code("mystery") is RecoveryCause.INTERNAL_FAILURE


@pytest.mark.parametrize(
    ("route", "expected_state"),
    [
        (CredentialRecoveryRoute.ENSURE, WorkerState.CHECKING_SESSION),
        (CredentialRecoveryRoute.REFRESH, WorkerState.REFRESHING_CREDENTIAL),
        (CredentialRecoveryRoute.VALIDATION_REFRESH, WorkerState.NEEDS_VALIDATION),
    ],
)
def test_retryable_credential_failure_retries_same_explicit_route(
    route: CredentialRecoveryRoute,
    expected_state: WorkerState,
) -> None:
    decision = RecoverySupervisor[object]().decide(
        RecoveryCause.CREDENTIAL_TRANSIENT_FAILURE,
        credential_route=route,
        attempt=3,
    )

    assert decision.credential_route is route
    assert decision.worker_state is expected_state
    assert decision.retry
    assert decision.retry_delay_s == 8.0


def test_transient_credential_failure_requires_route() -> None:
    with pytest.raises(ValueError):
        RecoverySupervisor[object]().decide(RecoveryCause.CREDENTIAL_TRANSIENT_FAILURE)


@pytest.mark.parametrize(
    ("route", "expected_state"),
    [
        (CredentialRecoveryRoute.ENSURE, WorkerState.CONNECTING),
        (CredentialRecoveryRoute.REFRESH, WorkerState.CHECKING_SESSION),
        (CredentialRecoveryRoute.VALIDATION_REFRESH, WorkerState.CHECKING_SESSION),
    ],
)
def test_successful_credential_recovery_selects_resume_state(
    route: CredentialRecoveryRoute,
    expected_state: WorkerState,
) -> None:
    decision = RecoverySupervisor[object]().decide(
        RecoveryCause.RECOVERY_SUCCEEDED,
        credential_route=route,
    )

    assert decision.action is RecoveryAction.RESUME
    assert decision.worker_state is expected_state
    assert decision.credential_route is CredentialRecoveryRoute.NONE
    assert not decision.retry


def test_successful_recovery_requires_completed_route() -> None:
    with pytest.raises(ValueError):
        RecoverySupervisor[object]().decide(RecoveryCause.RECOVERY_SUCCEEDED)


@dataclass
class _FakeCredentials:
    ensure_result: CredentialResult[str] = field(
        default_factory=lambda: _result(
            CredentialResultState.SUCCESS,
            secret="ensure-secret",
        )
    )
    refresh_result: CredentialResult[str] = field(
        default_factory=lambda: _result(
            CredentialResultState.SUCCESS,
            secret="refresh-secret",
        )
    )
    ensure_calls: list[str] = field(default_factory=list)
    refresh_calls: list[tuple[str, bool]] = field(default_factory=list)
    error: Exception | None = None

    async def ensure(self, account_id: str) -> CredentialResult[str]:
        self.ensure_calls.append(account_id)
        if self.error is not None:
            raise self.error
        return self.ensure_result

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[str]:
        self.refresh_calls.append((account_id, validation_recovery))
        if self.error is not None:
            raise self.error
        return self.refresh_result


async def test_recover_credential_ensure_routes_to_supervisor_ensure() -> None:
    credentials = _FakeCredentials()
    supervisor = RecoverySupervisor(credentials)

    outcome = await supervisor.recover_credential(
        "acct-1",
        route=CredentialRecoveryRoute.ENSURE,
    )

    assert credentials.ensure_calls == ["acct-1"]
    assert credentials.refresh_calls == []
    assert outcome.decision.action is RecoveryAction.RESUME
    assert outcome.decision.worker_state is WorkerState.CONNECTING
    assert outcome.credential_result is credentials.ensure_result


async def test_recover_credential_refresh_is_not_validation_recovery() -> None:
    credentials = _FakeCredentials()
    supervisor = RecoverySupervisor(credentials)

    outcome = await supervisor.recover_credential(
        "acct-1",
        route=CredentialRecoveryRoute.REFRESH,
    )

    assert credentials.refresh_calls == [("acct-1", False)]
    assert outcome.decision.worker_state is WorkerState.CHECKING_SESSION


async def test_validation_refresh_must_be_explicit_and_sets_validation_flag() -> None:
    credentials = _FakeCredentials()
    supervisor = RecoverySupervisor(credentials)

    outcome = await supervisor.recover_credential(
        "acct-1",
        route=CredentialRecoveryRoute.VALIDATION_REFRESH,
    )

    assert credentials.refresh_calls == [("acct-1", True)]
    assert outcome.decision.action is RecoveryAction.RESUME
    assert outcome.decision.worker_state is WorkerState.CHECKING_SESSION


async def test_retryable_credential_result_uses_bounded_backoff() -> None:
    credentials = _FakeCredentials(
        refresh_result=_result(
            CredentialResultState.RETRYABLE_FAILURE,
            code=CredentialFailureCode.NETWORK_ERROR,
        )
    )
    supervisor = RecoverySupervisor(credentials)

    outcome = await supervisor.recover_credential(
        "acct-1",
        route=CredentialRecoveryRoute.REFRESH,
        attempt=6,
        jitter_unit=1.0,
    )

    assert outcome.decision.cause is RecoveryCause.CREDENTIAL_TRANSIENT_FAILURE
    assert outcome.decision.credential_route is CredentialRecoveryRoute.REFRESH
    assert outcome.decision.retry
    assert outcome.decision.retry_delay_s == 60.0


async def test_validation_result_does_not_auto_route_validation_refresh() -> None:
    credentials = _FakeCredentials(
        ensure_result=_result(
            CredentialResultState.NEEDS_VALIDATION,
            code=CredentialFailureCode.NEEDS_VALIDATION,
        )
    )
    supervisor = RecoverySupervisor(credentials)

    outcome = await supervisor.recover_credential(
        "acct-1",
        route=CredentialRecoveryRoute.ENSURE,
    )

    assert credentials.ensure_calls == ["acct-1"]
    assert credentials.refresh_calls == []
    assert outcome.decision.action is RecoveryAction.WAIT_FOR_VALIDATION
    assert outcome.decision.worker_state is WorkerState.NEEDS_VALIDATION
    assert outcome.decision.credential_route is CredentialRecoveryRoute.NONE
    assert not outcome.decision.retry


async def test_terminal_credential_result_fails_closed() -> None:
    credentials = _FakeCredentials(
        ensure_result=_result(
            CredentialResultState.TERMINAL_FAILURE,
            code=CredentialFailureCode.CREDENTIAL_MISSING,
        )
    )

    outcome = await RecoverySupervisor(credentials).recover_credential(
        "acct-1",
        route=CredentialRecoveryRoute.ENSURE,
    )

    assert outcome.decision.cause is RecoveryCause.TERMINAL_FAILURE
    assert outcome.decision.worker_state is WorkerState.ERROR
    assert not outcome.decision.retry


async def test_internal_credential_result_maps_to_internal_failure() -> None:
    credentials = _FakeCredentials(
        ensure_result=_result(
            CredentialResultState.TERMINAL_FAILURE,
            code=CredentialFailureCode.INTERNAL_ERROR,
        )
    )

    outcome = await RecoverySupervisor(credentials).recover_credential(
        "acct-1",
        route=CredentialRecoveryRoute.ENSURE,
    )

    assert outcome.decision.cause is RecoveryCause.INTERNAL_FAILURE
    assert outcome.decision.worker_state is WorkerState.ERROR


async def test_unexpected_credential_port_exception_fails_closed() -> None:
    credentials = _FakeCredentials(error=RuntimeError("secret backend detail"))

    outcome = await RecoverySupervisor(credentials).recover_credential(
        "acct-1",
        route=CredentialRecoveryRoute.ENSURE,
    )

    assert outcome.decision.cause is RecoveryCause.INTERNAL_FAILURE
    assert outcome.decision.code == CredentialFailureCode.INTERNAL_ERROR.value
    assert outcome.credential_result is None
    assert "secret backend detail" not in repr(outcome)


async def test_execution_requires_configured_credential_port() -> None:
    with pytest.raises(RecoveryConfigurationError):
        await RecoverySupervisor[object]().recover_credential(
            "acct-1",
            route=CredentialRecoveryRoute.ENSURE,
        )


async def test_execution_rejects_none_route() -> None:
    with pytest.raises(ValueError):
        await RecoverySupervisor(_FakeCredentials()).recover_credential(
            "acct-1",
            route=CredentialRecoveryRoute.NONE,
        )


def test_decision_for_credential_result_rejects_none_route() -> None:
    with pytest.raises(ValueError):
        RecoverySupervisor[str]().decide_for_credential_result(
            _result(CredentialResultState.SUCCESS),
            route=CredentialRecoveryRoute.NONE,
        )


def test_recovery_outcome_repr_does_not_expose_credential_secret() -> None:
    credentials = _FakeCredentials(
        ensure_result=_result(
            CredentialResultState.SUCCESS,
            secret="TOP-SECRET-TOKEN",
        )
    )
    result = credentials.ensure_result
    decision = RecoverySupervisor[str]().decide_for_credential_result(
        result,
        route=CredentialRecoveryRoute.ENSURE,
    )

    assert "TOP-SECRET-TOKEN" not in repr(result)
    assert "TOP-SECRET-TOKEN" not in repr(decision)
