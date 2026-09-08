"""Credential health, refresh, and failure classification policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .health import CredentialHealth, CredentialHealthState, CredentialResultState
from .ports import (
    CredentialBackendError,
    CredentialBackendStatus,
    CredentialFailureCode,
    ValidationStatus,
)

_SAFE_MESSAGES: dict[CredentialFailureCode, str] = {
    CredentialFailureCode.CREDENTIAL_MISSING: "Credential is not available.",
    CredentialFailureCode.IDENTITY_MISSING: "Credential identity is incomplete.",
    CredentialFailureCode.SESSION_EXPIRED: "Credential session has expired.",
    CredentialFailureCode.NEEDS_VALIDATION: "Credential requires manual validation.",
    CredentialFailureCode.NETWORK_ERROR: "Credential refresh failed because of a network error.",
    CredentialFailureCode.ACCOUNT_NOT_FOUND: "Credential account does not exist.",
    CredentialFailureCode.AUTH_FAILED: "Credential authentication failed.",
    CredentialFailureCode.INTERNAL_ERROR: "Credential backend failed unexpectedly.",
}


@dataclass(frozen=True)
class CredentialPolicy:
    """Transport-neutral lifecycle policy."""

    early_refresh: timedelta = timedelta(minutes=5)

    def health(
        self,
        status: CredentialBackendStatus,
        validation: ValidationStatus,
        *,
        now: datetime | None = None,
    ) -> CredentialHealth:
        current = _as_utc(now or datetime.now(UTC))
        expires_at = status.expires_at
        code: str | None = None
        refresh_recommended = False
        validation_cooling = False

        if validation.required:
            state = CredentialHealthState.NEEDS_VALIDATION
            code = validation.code or CredentialFailureCode.NEEDS_VALIDATION.value
            expires_at = None
            validation_cooling = validation.cooling
        elif not status.cookie_available:
            state = CredentialHealthState.MISSING
            code = CredentialFailureCode.CREDENTIAL_MISSING.value
            expires_at = None
        elif not status.identity_available:
            state = CredentialHealthState.UNUSABLE
            code = CredentialFailureCode.IDENTITY_MISSING.value
            expires_at = None
        elif not status.token_cached or expires_at is None:
            state = CredentialHealthState.REFRESH_REQUIRED
            refresh_recommended = True
        else:
            expires_at = _as_utc(expires_at)
            if expires_at <= current:
                state = CredentialHealthState.EXPIRED
                refresh_recommended = True
            elif expires_at <= current + self.early_refresh:
                state = CredentialHealthState.EXPIRING
                refresh_recommended = True
            else:
                state = CredentialHealthState.HEALTHY

        return CredentialHealth(
            account_id=status.account_id,
            state=state,
            expires_at=expires_at,
            code=code,
            refresh_recommended=refresh_recommended,
            validation_cooling=validation_cooling,
        )

    def result_state_for(self, error: CredentialBackendError) -> CredentialResultState:
        if error.code is CredentialFailureCode.NEEDS_VALIDATION:
            return CredentialResultState.NEEDS_VALIDATION
        if error.retryable_hint or error.code is CredentialFailureCode.NETWORK_ERROR:
            return CredentialResultState.RETRYABLE_FAILURE
        return CredentialResultState.TERMINAL_FAILURE

    def safe_message(self, code: CredentialFailureCode) -> str:
        return _SAFE_MESSAGES[code]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
