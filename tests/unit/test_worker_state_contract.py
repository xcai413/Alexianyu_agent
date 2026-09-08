from __future__ import annotations

import pytest

from xianyu_agent.domain.runtime.worker_state import (
    InvalidWorkerStateTransition,
    UnknownWorkerStateValue,
    WorkerState,
    allowed_transitions_from,
    can_transition,
    deserialize_worker_state,
    legacy_worker_status_for,
    serialize_worker_state,
    transition_worker_state,
    worker_state_from_connection_state,
    worker_state_from_persistence,
)


def test_worker_state_contract_contains_required_states() -> None:
    assert [state.name for state in WorkerState] == [
        "DISABLED",
        "STARTING",
        "CHECKING_SESSION",
        "REFRESHING_CREDENTIAL",
        "CONNECTING",
        "REGISTERING",
        "SYNCING",
        "ONLINE",
        "RECONNECTING",
        "NEEDS_VALIDATION",
        "STOPPING",
        "ERROR",
    ]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkerState.DISABLED, WorkerState.STARTING),
        (WorkerState.STARTING, WorkerState.CHECKING_SESSION),
        (WorkerState.CHECKING_SESSION, WorkerState.CONNECTING),
        (WorkerState.CONNECTING, WorkerState.REGISTERING),
        (WorkerState.REGISTERING, WorkerState.SYNCING),
        (WorkerState.SYNCING, WorkerState.ONLINE),
        (WorkerState.ONLINE, WorkerState.RECONNECTING),
        (WorkerState.RECONNECTING, WorkerState.CONNECTING),
        (WorkerState.ONLINE, WorkerState.STOPPING),
        (WorkerState.STOPPING, WorkerState.DISABLED),
        (WorkerState.CONNECTING, WorkerState.ERROR),
        (WorkerState.ERROR, WorkerState.STARTING),
    ],
)
def test_legal_transitions_are_accepted(
    current: WorkerState,
    target: WorkerState,
) -> None:
    assert can_transition(current, target)
    assert transition_worker_state(current, target) is target


def test_credential_refresh_returns_to_session_check() -> None:
    assert (
        transition_worker_state(
            WorkerState.CHECKING_SESSION,
            WorkerState.REFRESHING_CREDENTIAL,
        )
        is WorkerState.REFRESHING_CREDENTIAL
    )
    assert (
        transition_worker_state(
            WorkerState.REFRESHING_CREDENTIAL,
            WorkerState.CHECKING_SESSION,
        )
        is WorkerState.CHECKING_SESSION
    )


def test_validation_gate_requires_recheck_before_online() -> None:
    assert can_transition(WorkerState.REGISTERING, WorkerState.NEEDS_VALIDATION)
    assert can_transition(WorkerState.NEEDS_VALIDATION, WorkerState.CHECKING_SESSION)
    assert not can_transition(WorkerState.NEEDS_VALIDATION, WorkerState.ONLINE)


def test_self_transition_is_idempotent_but_not_a_declared_next_state() -> None:
    assert can_transition(WorkerState.ONLINE, WorkerState.ONLINE)
    assert WorkerState.ONLINE not in allowed_transitions_from(WorkerState.ONLINE)
    assert transition_worker_state(WorkerState.ONLINE, WorkerState.ONLINE) is WorkerState.ONLINE


def test_invalid_transition_raises_typed_error() -> None:
    with pytest.raises(InvalidWorkerStateTransition) as exc_info:
        transition_worker_state(WorkerState.DISABLED, WorkerState.ONLINE)

    assert exc_info.value.current is WorkerState.DISABLED
    assert exc_info.value.target is WorkerState.ONLINE
    assert "disabled -> online" in str(exc_info.value)


@pytest.mark.parametrize("state", list(WorkerState))
def test_canonical_serialization_round_trip(state: WorkerState) -> None:
    encoded = serialize_worker_state(state)
    assert encoded == state.value
    assert deserialize_worker_state(encoded) is state
    assert deserialize_worker_state(encoded.upper()) is state


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("offline", WorkerState.DISABLED),
        ("idle", WorkerState.DISABLED),
        ("disconnected", WorkerState.DISABLED),
        ("stopped", WorkerState.DISABLED),
        ("no_worker", WorkerState.DISABLED),
        ("connected", WorkerState.SYNCING),
        ("risk_cooling", WorkerState.NEEDS_VALIDATION),
        ("reconnecting", WorkerState.RECONNECTING),
        ("error", WorkerState.ERROR),
        ("online", WorkerState.ONLINE),
        ("registering", WorkerState.REGISTERING),
    ],
)
def test_persistence_compatibility_mapping(
    legacy: str,
    expected: WorkerState,
) -> None:
    assert worker_state_from_persistence(legacy) is expected


def test_durable_validation_flag_overrides_stale_status_string() -> None:
    assert (
        worker_state_from_persistence(
            "online",
            risk_recovery_required=True,
        )
        is WorkerState.NEEDS_VALIDATION
    )


@pytest.mark.parametrize(
    ("connection_state", "expected"),
    [
        ("idle", WorkerState.DISABLED),
        ("connecting", WorkerState.CONNECTING),
        ("connected", WorkerState.SYNCING),
        ("reconnecting", WorkerState.RECONNECTING),
        ("disconnected", WorkerState.DISABLED),
        ("error", WorkerState.ERROR),
    ],
)
def test_connection_state_compatibility_mapping(
    connection_state: str,
    expected: WorkerState,
) -> None:
    assert worker_state_from_connection_state(connection_state) is expected


def test_protocol_connected_is_not_promoted_to_online() -> None:
    assert worker_state_from_connection_state("connected") is WorkerState.SYNCING
    assert worker_state_from_connection_state("connected") is not WorkerState.ONLINE


@pytest.mark.parametrize(
    ("state", "legacy"),
    [
        (WorkerState.DISABLED, "offline"),
        (WorkerState.STARTING, "connecting"),
        (WorkerState.CHECKING_SESSION, "connecting"),
        (WorkerState.REFRESHING_CREDENTIAL, "connecting"),
        (WorkerState.CONNECTING, "connecting"),
        (WorkerState.REGISTERING, "connecting"),
        (WorkerState.SYNCING, "connected"),
        (WorkerState.ONLINE, "online"),
        (WorkerState.RECONNECTING, "reconnecting"),
        (WorkerState.NEEDS_VALIDATION, "risk_cooling"),
        (WorkerState.STOPPING, "offline"),
        (WorkerState.ERROR, "error"),
    ],
)
def test_legacy_status_projection_is_explicitly_lossy(
    state: WorkerState,
    legacy: str,
) -> None:
    assert legacy_worker_status_for(state) == legacy


@pytest.mark.parametrize("value", ["running", "", "mystery"])
def test_unknown_persistence_values_fail_closed(value: str) -> None:
    with pytest.raises(UnknownWorkerStateValue):
        worker_state_from_persistence(value)


def test_unknown_connection_state_fails_closed() -> None:
    with pytest.raises(UnknownWorkerStateValue):
        worker_state_from_connection_state("subscription_ready")


def test_canonical_deserializer_does_not_silently_accept_legacy_connected() -> None:
    with pytest.raises(UnknownWorkerStateValue):
        deserialize_worker_state("connected")
