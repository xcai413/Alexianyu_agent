"""Application orchestration for the QR login use case.

The application layer coordinates account preconditions, QR platform interaction,
credential persistence, and post-login credential validation.  It intentionally owns
no QR rendering and imports no protocol implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from .health import CredentialResult, CredentialResultState

QrStatusCallback = Callable[[str], None]


class QrLoginResultState(StrEnum):
    """Stable outcomes for a completed QR login orchestration."""

    SUCCESS = "success"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    VERIFICATION_REQUIRED = "verification_required"
    INVALID_SESSION = "invalid_session"
    PERSISTENCE_FAILURE = "persistence_failure"
    NEEDS_VALIDATION = "needs_validation"
    CREDENTIAL_RETRYABLE_FAILURE = "credential_retryable_failure"
    CREDENTIAL_TERMINAL_FAILURE = "credential_terminal_failure"


class QrLoginAccountBusyError(RuntimeError):
    """Raised when QR login would race a desired-running account worker."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        super().__init__(f"account {account_id} is desired running")


class QrLoginInvalidChallengeError(RuntimeError):
    """Raised when the platform did not provide renderable QR content."""


class QrAccountView(Protocol):
    desired_state: str


class QrAccountStore(Protocol):
    async def get_account(self, account_id: str) -> QrAccountView | None: ...

    async def create_account(
        self,
        account_id: str,
        *,
        remark: str | None = None,
    ) -> object: ...


class QrCookieStore(Protocol):
    async def save_cookie(self, account_id: str, cookie: str) -> bool: ...


class QrCredentialValidator(Protocol):
    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[Any]: ...


class QrLoginPlatform(Protocol):
    async def generate(self) -> Any: ...

    async def wait_for_login(
        self,
        session: Any,
        *,
        timeout_s: float,
        on_status: QrStatusCallback | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class QrLoginChallenge:
    """Render-safe QR challenge returned to interface adapters.

    The platform session carries cookies and platform parameters, so it is deliberately
    excluded from repr/compare and can only be handed back to this use case.
    """

    account_id: str
    session_id: str
    qr_content: str
    _session: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class QrLoginResult:
    """Secret-free result consumed by CLI/API adapters."""

    state: QrLoginResultState
    account_id: str
    session_id: str
    status: str
    cookie_saved: bool = False
    verification_url: str | None = None
    credential_code: str | None = None
    credential_expires_at: datetime | None = None
    validation_cooling: bool = False
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is QrLoginResultState.SUCCESS


class QrLoginApplication:
    """Coordinate one manual QR login without UI or Runtime/Worker state coupling."""

    def __init__(
        self,
        *,
        platform: QrLoginPlatform,
        accounts: QrAccountStore,
        cookies: QrCookieStore,
        credentials: QrCredentialValidator,
    ) -> None:
        self._platform = platform
        self._accounts = accounts
        self._cookies = cookies
        self._credentials = credentials

    async def start(self, account_id: str) -> QrLoginChallenge:
        """Validate account preconditions and generate a renderable QR challenge."""
        existing = await self._accounts.get_account(account_id)
        if existing is not None and existing.desired_state == "running":
            raise QrLoginAccountBusyError(account_id)

        session = await self._platform.generate()
        qr_content = getattr(session, "qr_content", None)
        session_id = getattr(session, "session_id", None)
        if not isinstance(qr_content, str) or not qr_content:
            raise QrLoginInvalidChallengeError("QR platform returned no code content")
        if not isinstance(session_id, str) or not session_id:
            raise QrLoginInvalidChallengeError("QR platform returned no session id")
        return QrLoginChallenge(
            account_id=account_id,
            session_id=session_id,
            qr_content=qr_content,
            _session=session,
        )

    async def finish(
        self,
        challenge: QrLoginChallenge,
        *,
        remark: str = "",
        timeout_s: float,
        on_status: QrStatusCallback | None = None,
    ) -> QrLoginResult:
        """Wait for confirmation, persist the Cookie, then validate credentials.

        ``validation_recovery=True`` is intentional: the already-established
        CredentialSupervisor remains authoritative for persisted validation cooldowns.
        This flow never follows verification URLs or attempts to bypass platform risk
        controls.
        """
        final = await self._platform.wait_for_login(
            challenge._session,
            timeout_s=timeout_s,
            on_status=on_status,
        )
        session = challenge._session

        if final == "verification_required":
            verification_url = getattr(session, "verification_url", None)
            return self._result(
                challenge,
                QrLoginResultState.VERIFICATION_REQUIRED,
                final,
                verification_url=(
                    verification_url if isinstance(verification_url, str) else None
                ),
            )
        if final == "expired":
            return self._result(challenge, QrLoginResultState.EXPIRED, final)
        if final != "success":
            return self._result(challenge, QrLoginResultState.CANCELLED, final)

        unb = getattr(session, "unb", None)
        cookie_string = getattr(session, "cookie_string", None)
        if not isinstance(unb, str) or not unb or not callable(cookie_string):
            return self._result(
                challenge,
                QrLoginResultState.INVALID_SESSION,
                final,
                message="QR login succeeded without a usable identity",
            )

        await self._accounts.create_account(
            challenge.account_id,
            remark=remark or None,
        )
        if not await self._cookies.save_cookie(
            challenge.account_id,
            cookie_string(),
        ):
            return self._result(
                challenge,
                QrLoginResultState.PERSISTENCE_FAILURE,
                final,
                message="QR login Cookie could not be persisted",
            )

        credential = await self._credentials.refresh(
            challenge.account_id,
            validation_recovery=True,
        )
        if credential.state is CredentialResultState.SUCCESS:
            return self._result(
                challenge,
                QrLoginResultState.SUCCESS,
                final,
                cookie_saved=True,
                credential_expires_at=credential.health.expires_at,
            )
        if credential.state is CredentialResultState.NEEDS_VALIDATION:
            return self._result(
                challenge,
                QrLoginResultState.NEEDS_VALIDATION,
                final,
                cookie_saved=True,
                credential_code=credential.code,
                validation_cooling=credential.health.validation_cooling,
                message=credential.message,
            )
        if credential.state is CredentialResultState.RETRYABLE_FAILURE:
            result_state = QrLoginResultState.CREDENTIAL_RETRYABLE_FAILURE
        else:
            result_state = QrLoginResultState.CREDENTIAL_TERMINAL_FAILURE
        return self._result(
            challenge,
            result_state,
            final,
            cookie_saved=True,
            credential_code=credential.code,
            message=credential.message,
        )

    @staticmethod
    def _result(
        challenge: QrLoginChallenge,
        state: QrLoginResultState,
        status: str,
        **kwargs: Any,
    ) -> QrLoginResult:
        return QrLoginResult(
            state=state,
            account_id=challenge.account_id,
            session_id=challenge.session_id,
            status=status,
            **kwargs,
        )
