"""Canonical Worker State contract and compatibility mappings.

This module deliberately has no protocol, ORM, or AccountWorker dependency. It
defines the lifecycle vocabulary that later runtime integration can consume
without coupling the domain contract to the legacy WebSocket client.

The existing ``worker_status.status`` column is a free-form ``String(32)`` and
currently contains a mix of legacy worker statuses and protocol connection
states. Compatibility helpers below normalize those values without changing the
database schema in this slice.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class WorkerState(StrEnum):
    """Canonical lifecycle state for one account worker."""

    DISABLED = "disabled"
    STARTING = "starting"
    CHECKING_SESSION = "checking_session"
    REFRESHING_CREDENTIAL = "refreshing_credential"
    CONNECTING = "connecting"
    REGISTERING = "registering"
    SYNCING = "syncing"
    ONLINE = "online"
    RECONNECTING = "reconnecting"
    NEEDS_VALIDATION = "needs_validation"
    STOPPING = "stopping"
    ERROR = "error"


class InvalidWorkerStateTransition(ValueError):
    """Raised when a caller attempts a lifecycle transition that is not allowed."""

    def __init__(self, current: WorkerState, target: WorkerState) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"invalid worker state transition: {current.value} -> {target.value}"
        )


class UnknownWorkerStateValue(ValueError):
    """Raised when a persisted or compatibility value cannot be normalized."""

    def __init__(self, value: object, *, source: str) -> None:
        self.value = value
        self.source = source
        super().__init__(f"unknown {source} worker state value: {value!r}")


_TRANSITIONS: dict[WorkerState, frozenset[WorkerState]] = {
    WorkerState.DISABLED: frozenset({WorkerState.STARTING}),
    WorkerState.STARTING: frozenset(
        {
            WorkerState.CHECKING_SESSION,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.STOPPING,
            WorkerState.ERROR,
        }
    ),
    WorkerState.CHECKING_SESSION: frozenset(
        {
            WorkerState.REFRESHING_CREDENTIAL,
            WorkerState.CONNECTING,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.STOPPING,
            WorkerState.ERROR,
        }
    ),
    WorkerState.REFRESHING_CREDENTIAL: frozenset(
        {
            WorkerState.CHECKING_SESSION,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.STOPPING,
            WorkerState.ERROR,
        }
    ),
    WorkerState.CONNECTING: frozenset(
        {
            WorkerState.REGISTERING,
            WorkerState.RECONNECTING,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.STOPPING,
            WorkerState.ERROR,
        }
    ),
    WorkerState.REGISTERING: frozenset(
        {
            WorkerState.SYNCING,
            WorkerState.RECONNECTING,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.STOPPING,
            WorkerState.ERROR,
        }
    ),
    WorkerState.SYNCING: frozenset(
        {
            WorkerState.ONLINE,
            WorkerState.RECONNECTING,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.STOPPING,
            WorkerState.ERROR,
        }
    ),
    WorkerState.ONLINE: frozenset(
        {
            WorkerState.RECONNECTING,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.STOPPING,
            WorkerState.ERROR,
        }
    ),
    WorkerState.RECONNECTING: frozenset(
        {
            WorkerState.CHECKING_SESSION,
            WorkerState.REFRESHING_CREDENTIAL,
            WorkerState.CONNECTING,
            WorkerState.NEEDS_VALIDATION,
            WorkerState.STOPPING,
            WorkerState.ERROR,
        }
    ),
    WorkerState.NEEDS_VALIDATION: frozenset(
        {
            WorkerState.CHECKING_SESSION,
            WorkerState.STOPPING,
            WorkerState.DISABLED,
        }
    ),
    WorkerState.STOPPING: frozenset(
        {
            WorkerState.DISABLED,
            WorkerState.ERROR,
        }
    ),
    WorkerState.ERROR: frozenset(
        {
            WorkerState.STARTING,
            WorkerState.STOPPING,
            WorkerState.DISABLED,
        }
    ),
}

ALLOWED_TRANSITIONS: Mapping[WorkerState, frozenset[WorkerState]] = MappingProxyType(
    _TRANSITIONS
)


def allowed_transitions_from(state: WorkerState) -> frozenset[WorkerState]:
    """Return the explicit next states for ``state``.

    A self-transition is an idempotent no-op and is therefore accepted by
    :func:`can_transition`, but it is intentionally not included here.
    """

    return ALLOWED_TRANSITIONS[state]


def can_transition(current: WorkerState, target: WorkerState) -> bool:
    """Return whether ``current -> target`` is valid.

    Re-emitting the same state is accepted as an idempotent no-op. This keeps
    heartbeat/persistence writers from manufacturing lifecycle errors when they
    observe the same state more than once.
    """

    return current is target or target in ALLOWED_TRANSITIONS[current]


def transition_worker_state(current: WorkerState, target: WorkerState) -> WorkerState:
    """Validate and return ``target`` or raise ``InvalidWorkerStateTransition``."""

    if not can_transition(current, target):
        raise InvalidWorkerStateTransition(current, target)
    return target


def serialize_worker_state(state: WorkerState) -> str:
    """Serialize the canonical state for JSON or future persistence."""

    return state.value


def deserialize_worker_state(value: str) -> WorkerState:
    """Deserialize a canonical serialized value.

    Legacy values such as ``connected`` and ``risk_cooling`` must use
    :func:`worker_state_from_persistence`, which makes compatibility explicit.
    """

    normalized = _normalize(value, source="canonical")
    try:
        return WorkerState(normalized)
    except ValueError as exc:
        raise UnknownWorkerStateValue(value, source="canonical") from exc


_LEGACY_STATUS_TO_STATE: Mapping[str, WorkerState] = MappingProxyType(
    {
        # Existing worker_status / AccountPool compatibility values.
        "offline": WorkerState.DISABLED,
        "idle": WorkerState.DISABLED,
        "disconnected": WorkerState.DISABLED,
        "stopped": WorkerState.DISABLED,
        "no_worker": WorkerState.DISABLED,
        # Legacy ``connected`` only proves the current WsClient transport /
        # registration path. Issue #3 explicitly requires ONLINE to mean the
        # stronger subscription-ready state, so do not promote it to ONLINE.
        "connected": WorkerState.SYNCING,
        "risk_cooling": WorkerState.NEEDS_VALIDATION,
    }
)


def worker_state_from_persistence(
    value: str | WorkerState,
    *,
    risk_recovery_required: bool = False,
) -> WorkerState:
    """Normalize canonical or legacy ``worker_status.status`` values.

    ``risk_recovery_required`` has precedence because the durable risk flag is
    more authoritative than a stale status string.
    """

    if risk_recovery_required:
        return WorkerState.NEEDS_VALIDATION
    if isinstance(value, WorkerState):
        return value
    normalized = _normalize(value, source="persistence")
    try:
        return WorkerState(normalized)
    except ValueError:
        pass
    mapped = _LEGACY_STATUS_TO_STATE.get(normalized)
    if mapped is None:
        raise UnknownWorkerStateValue(value, source="persistence")
    return mapped


_CONNECTION_STATE_TO_WORKER_STATE: Mapping[str, WorkerState] = MappingProxyType(
    {
        "idle": WorkerState.DISABLED,
        "connecting": WorkerState.CONNECTING,
        "connected": WorkerState.SYNCING,
        "reconnecting": WorkerState.RECONNECTING,
        "disconnected": WorkerState.DISABLED,
        "error": WorkerState.ERROR,
    }
)


def worker_state_from_connection_state(value: str) -> WorkerState:
    """Map the current protocol ``ConnectionState`` vocabulary conservatively.

    In particular, ``connected`` maps to ``SYNCING`` rather than ``ONLINE``.
    The canonical contract reserves ``ONLINE`` for a later subscription-ready
    signal, preventing transport connectivity from being mistaken for readiness.
    """

    normalized = _normalize(value, source="connection")
    mapped = _CONNECTION_STATE_TO_WORKER_STATE.get(normalized)
    if mapped is None:
        raise UnknownWorkerStateValue(value, source="connection")
    return mapped


_STATE_TO_LEGACY_STATUS: Mapping[WorkerState, str] = MappingProxyType(
    {
        WorkerState.DISABLED: "offline",
        WorkerState.STARTING: "connecting",
        WorkerState.CHECKING_SESSION: "connecting",
        WorkerState.REFRESHING_CREDENTIAL: "connecting",
        WorkerState.CONNECTING: "connecting",
        WorkerState.REGISTERING: "connecting",
        WorkerState.SYNCING: "connected",
        WorkerState.ONLINE: "online",
        WorkerState.RECONNECTING: "reconnecting",
        WorkerState.NEEDS_VALIDATION: "risk_cooling",
        WorkerState.STOPPING: "offline",
        WorkerState.ERROR: "error",
    }
)


def legacy_worker_status_for(state: WorkerState) -> str:
    """Return the lossy status string understood by existing status consumers."""

    return _STATE_TO_LEGACY_STATUS[state]


def _normalize(value: str, *, source: str) -> str:
    if not isinstance(value, str):
        raise UnknownWorkerStateValue(value, source=source)
    normalized = value.strip().lower()
    if not normalized:
        raise UnknownWorkerStateValue(value, source=source)
    return normalized
