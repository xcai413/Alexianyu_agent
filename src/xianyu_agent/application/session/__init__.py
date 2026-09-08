"""Credential and login lifecycle application boundary."""

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
    CredentialBackendStatus,
    CredentialFailureCode,
    ValidationGate,
    ValidationStatus,
)
from .qr_login import (
    QrLoginAccountBusyError,
    QrLoginApplication,
    QrLoginChallenge,
    QrLoginInvalidChallengeError,
    QrLoginResult,
    QrLoginResultState,
)
from .supervisor import CredentialSupervisor

__all__ = [
    "CredentialBackend",
    "CredentialBackendError",
    "CredentialBackendStatus",
    "CredentialFailureCode",
    "CredentialHandle",
    "CredentialHealth",
    "CredentialHealthState",
    "CredentialPolicy",
    "CredentialResult",
    "CredentialResultState",
    "CredentialSupervisor",
    "QrLoginAccountBusyError",
    "QrLoginApplication",
    "QrLoginChallenge",
    "QrLoginInvalidChallengeError",
    "QrLoginResult",
    "QrLoginResultState",
    "ValidationGate",
    "ValidationStatus",
]
