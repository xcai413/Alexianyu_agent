"""Unit tests for QR login application orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from xianyu_agent.application.session.health import (
    CredentialHealth,
    CredentialHealthState,
    CredentialResult,
    CredentialResultState,
)
from xianyu_agent.application.session.qr_login import (
    QrLoginAccountBusyError,
    QrLoginApplication,
    QrLoginResultState,
)

ACCOUNT_ID = "account-1"


@dataclass
class FakeAccount:
    desired_state: str = "stopped"


class FakeAccounts:
    def __init__(self, account: FakeAccount | None = None) -> None:
        self.account = account
        self.created: list[tuple[str, str | None]] = []

    async def get_account(self, account_id: str) -> FakeAccount | None:
        assert account_id == ACCOUNT_ID
        return self.account

    async def create_account(
        self,
        account_id: str,
        *,
        remark: str | None = None,
    ) -> object:
        self.created.append((account_id, remark))
        self.account = FakeAccount()
        return self.account


class FakeSession:
    def __init__(self) -> None:
        self.session_id = "session-secret-id"
        self.status = "waiting"
        self.qr_content = "https://qr.example/secret-code"
        self.unb: str | None = "2214350705775"
        self.verification_url: str | None = None
        self.cookies = {"cookie2": "secret-cookie", "unb": self.unb}

    def cookie_string(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self.cookies.items())


class FakePlatform:
    def __init__(self, *, final: str = "success") -> None:
        self.session = FakeSession()
        self.final = final
        self.generate_calls = 0
        self.wait_calls = 0
        self.statuses: list[str] = []

    async def generate(self) -> FakeSession:
        self.generate_calls += 1
        return self.session

    async def wait_for_login(
        self,
        session: FakeSession,
        *,
        timeout_s: float,
        on_status=None,
    ) -> str:
        assert session is self.session
        assert timeout_s > 0
        self.wait_calls += 1
        for status in self.statuses:
            if on_status is not None:
                on_status(status)
        session.status = self.final
        return self.final


class FakeCookies:
    def __init__(self, *, saved: bool = True) -> None:
        self.saved = saved
        self.calls: list[tuple[str, str]] = []

    async def save_cookie(self, account_id: str, cookie: str) -> bool:
        self.calls.append((account_id, cookie))
        return self.saved


class FakeCredentials:
    def __init__(self, result: CredentialResult[object] | None = None) -> None:
        self.result = result or _credential_result(CredentialResultState.SUCCESS)
        self.calls: list[tuple[str, bool]] = []

    async def refresh(
        self,
        account_id: str,
        *,
        validation_recovery: bool = False,
    ) -> CredentialResult[object]:
        self.calls.append((account_id, validation_recovery))
        return self.result


def _credential_result(
    state: CredentialResultState,
    *,
    cooling: bool = False,
    code: str | None = None,
) -> CredentialResult[object]:
    if state is CredentialResultState.SUCCESS:
        health_state = CredentialHealthState.HEALTHY
        expires_at = datetime.now(UTC) + timedelta(hours=8)
    elif state is CredentialResultState.NEEDS_VALIDATION:
        health_state = CredentialHealthState.NEEDS_VALIDATION
        expires_at = None
    else:
        health_state = CredentialHealthState.ERROR
        expires_at = None
    return CredentialResult(
        state=state,
        health=CredentialHealth(
            account_id=ACCOUNT_ID,
            state=health_state,
            expires_at=expires_at,
            validation_cooling=cooling,
        ),
        code=code,
        message="safe credential result",
    )


def _application(
    *,
    platform: FakePlatform | None = None,
    accounts: FakeAccounts | None = None,
    cookies: FakeCookies | None = None,
    credentials: FakeCredentials | None = None,
) -> tuple[QrLoginApplication, FakePlatform, FakeAccounts, FakeCookies, FakeCredentials]:
    platform = platform or FakePlatform()
    accounts = accounts or FakeAccounts()
    cookies = cookies or FakeCookies()
    credentials = credentials or FakeCredentials()
    return (
        QrLoginApplication(
            platform=platform,
            accounts=accounts,
            cookies=cookies,
            credentials=credentials,
        ),
        platform,
        accounts,
        cookies,
        credentials,
    )


@pytest.mark.asyncio
async def test_start_blocks_desired_running_account_before_platform_call() -> None:
    app, platform, _, _, _ = _application(
        accounts=FakeAccounts(FakeAccount(desired_state="running"))
    )

    with pytest.raises(QrLoginAccountBusyError):
        await app.start(ACCOUNT_ID)

    assert platform.generate_calls == 0


@pytest.mark.asyncio
async def test_start_returns_render_safe_challenge() -> None:
    app, _, _, _, _ = _application()

    challenge = await app.start(ACCOUNT_ID)

    assert challenge.account_id == ACCOUNT_ID
    assert challenge.session_id == "session-secret-id"
    assert challenge.qr_content == "https://qr.example/secret-code"
    rendered = repr(challenge)
    assert "secret-cookie" not in rendered
    assert "2214350705775" not in rendered


@pytest.mark.asyncio
async def test_success_persists_cookie_and_uses_explicit_validation_recovery() -> None:
    app, _, accounts, cookies, credentials = _application()
    challenge = await app.start(ACCOUNT_ID)

    result = await app.finish(challenge, remark="shop", timeout_s=30.0)

    assert result.state is QrLoginResultState.SUCCESS
    assert result.cookie_saved is True
    assert result.credential_expires_at is not None
    assert accounts.created == [(ACCOUNT_ID, "shop")]
    assert cookies.calls == [
        (ACCOUNT_ID, "cookie2=secret-cookie; unb=2214350705775")
    ]
    assert credentials.calls == [(ACCOUNT_ID, True)]
    assert "secret-cookie" not in repr(result)


@pytest.mark.asyncio
async def test_verification_required_is_returned_without_following_or_persisting() -> None:
    platform = FakePlatform(final="verification_required")
    platform.session.verification_url = "https://passport.goofish.com/verify"
    app, _, accounts, cookies, credentials = _application(platform=platform)
    challenge = await app.start(ACCOUNT_ID)

    result = await app.finish(challenge, timeout_s=30.0)

    assert result.state is QrLoginResultState.VERIFICATION_REQUIRED
    assert result.verification_url == "https://passport.goofish.com/verify"
    assert accounts.created == []
    assert cookies.calls == []
    assert credentials.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final", "expected"),
    [
        ("expired", QrLoginResultState.EXPIRED),
        ("cancelled", QrLoginResultState.CANCELLED),
    ],
)
async def test_non_success_terminal_status_does_not_persist(
    final: str,
    expected: QrLoginResultState,
) -> None:
    app, _, accounts, cookies, credentials = _application(
        platform=FakePlatform(final=final)
    )
    challenge = await app.start(ACCOUNT_ID)

    result = await app.finish(challenge, timeout_s=30.0)

    assert result.state is expected
    assert accounts.created == []
    assert cookies.calls == []
    assert credentials.calls == []


@pytest.mark.asyncio
async def test_success_without_unb_is_rejected_before_persistence() -> None:
    platform = FakePlatform()
    platform.session.unb = None
    app, _, accounts, cookies, credentials = _application(platform=platform)
    challenge = await app.start(ACCOUNT_ID)

    result = await app.finish(challenge, timeout_s=30.0)

    assert result.state is QrLoginResultState.INVALID_SESSION
    assert accounts.created == []
    assert cookies.calls == []
    assert credentials.calls == []


@pytest.mark.asyncio
async def test_cookie_persistence_failure_stops_before_credential_refresh() -> None:
    app, _, accounts, cookies, credentials = _application(cookies=FakeCookies(saved=False))
    challenge = await app.start(ACCOUNT_ID)

    result = await app.finish(challenge, timeout_s=30.0)

    assert result.state is QrLoginResultState.PERSISTENCE_FAILURE
    assert accounts.created == [(ACCOUNT_ID, None)]
    assert len(cookies.calls) == 1
    assert credentials.calls == []


@pytest.mark.asyncio
async def test_validation_cooldown_remains_authoritative_after_cookie_save() -> None:
    credentials = FakeCredentials(
        _credential_result(
            CredentialResultState.NEEDS_VALIDATION,
            cooling=True,
            code="NEEDS_VALIDATION",
        )
    )
    app, _, _, cookies, credentials = _application(credentials=credentials)
    challenge = await app.start(ACCOUNT_ID)

    result = await app.finish(challenge, timeout_s=30.0)

    assert result.state is QrLoginResultState.NEEDS_VALIDATION
    assert result.cookie_saved is True
    assert result.validation_cooling is True
    assert len(cookies.calls) == 1
    assert credentials.calls == [(ACCOUNT_ID, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_state", "expected"),
    [
        (
            CredentialResultState.RETRYABLE_FAILURE,
            QrLoginResultState.CREDENTIAL_RETRYABLE_FAILURE,
        ),
        (
            CredentialResultState.TERMINAL_FAILURE,
            QrLoginResultState.CREDENTIAL_TERMINAL_FAILURE,
        ),
    ],
)
async def test_credential_failures_preserve_saved_cookie(
    credential_state: CredentialResultState,
    expected: QrLoginResultState,
) -> None:
    credentials = FakeCredentials(
        _credential_result(credential_state, code="AUTH_FAILED")
    )
    app, _, _, cookies, _ = _application(credentials=credentials)
    challenge = await app.start(ACCOUNT_ID)

    result = await app.finish(challenge, timeout_s=30.0)

    assert result.state is expected
    assert result.cookie_saved is True
    assert result.credential_code == "AUTH_FAILED"
    assert len(cookies.calls) == 1


@pytest.mark.asyncio
async def test_status_callback_is_forwarded_to_platform_wait() -> None:
    platform = FakePlatform()
    platform.statuses = ["scanned", "success"]
    app, _, _, _, _ = _application(platform=platform)
    challenge = await app.start(ACCOUNT_ID)
    observed: list[str] = []

    await app.finish(challenge, timeout_s=30.0, on_status=observed.append)

    assert observed == ["scanned", "success"]
