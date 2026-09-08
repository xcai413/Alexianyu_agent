from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from xianyu_agent.application.session import (
    CredentialBackendError,
    CredentialFailureCode,
    CredentialPolicy,
    CredentialResultState,
)
from xianyu_agent.domain.account.risk import WorkerRiskCircuit
from xianyu_agent.infrastructure.session.ws_credentials import (
    LegacyValidationGate,
    LegacyWsCredentialBackend,
)
from xianyu_agent.protocol.ws_auth import (
    WsAuthError,
    WsCredentials,
    WsCredentialStatus,
    _safe_token_error,
)

ACCOUNT_ID = "account-1"


class FakeSigner:
    def __init__(self, fingerprint: dict[str, object] | None) -> None:
        self.value = fingerprint

    async def fingerprint(self, account_id: str) -> dict[str, object] | None:
        assert account_id == ACCOUNT_ID
        return self.value


class FakeProvider:
    def __init__(self) -> None:
        self.status_value = WsCredentialStatus(
            account_id=ACCOUNT_ID,
            exists=True,
            token_cached=True,
            valid=True,
            device_id_masked="masked",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.credentials = WsCredentials(
            access_token="secret-token",
            device_id="secret-device",
            user_id="secret-user",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.error: WsAuthError | None = None
        self.force_refresh_calls: list[bool] = []

    async def status(self, account_id: str) -> WsCredentialStatus:
        assert account_id == ACCOUNT_ID
        return self.status_value

    async def get_credentials(
        self,
        account_id: str,
        *,
        force_refresh: bool = False,
    ) -> WsCredentials:
        assert account_id == ACCOUNT_ID
        self.force_refresh_calls.append(force_refresh)
        if self.error is not None:
            raise self.error
        return self.credentials


async def test_legacy_backend_exposes_only_secret_free_status() -> None:
    signer = FakeSigner(
        {
            "has_unb": True,
            "unb_masked": "ab***yz#digest",
            "m_h5_tk_digest": "digest",
        }
    )
    provider = FakeProvider()
    backend = LegacyWsCredentialBackend(signer=signer, provider=provider)

    status = await backend.inspect(ACCOUNT_ID)

    assert status.cookie_available is True
    assert status.identity_available is True
    assert status.token_cached is True
    assert "secret-token" not in repr(status)
    assert "secret-user" not in repr(status)


async def test_legacy_backend_preserves_force_refresh_behavior() -> None:
    provider = FakeProvider()
    backend = LegacyWsCredentialBackend(
        signer=FakeSigner({"has_unb": True}),
        provider=provider,
    )

    credentials = await backend.acquire(ACCOUNT_ID, force_refresh=True)

    assert credentials is provider.credentials
    assert provider.force_refresh_calls == [True]


@pytest.mark.parametrize(
    ("message", "expected_code", "retryable"),
    [
        (
            "FAIL_SYS_USER_VALIDATE: validation required",
            CredentialFailureCode.NEEDS_VALIDATION,
            False,
        ),
        ("WS Token 网络请求失败(ReadTimeout)", CredentialFailureCode.NETWORK_ERROR, True),
        ("账号 account-1 无可用 Cookie", CredentialFailureCode.CREDENTIAL_MISSING, False),
        ("账号 account-1 Cookie 缺少 unb,请重新扫码登录", CredentialFailureCode.IDENTITY_MISSING, False),
        ("FAIL_SYS_SESSION_EXPIRED", CredentialFailureCode.SESSION_EXPIRED, False),
        ("账号 account-1 不存在", CredentialFailureCode.ACCOUNT_NOT_FOUND, False),
        ("unknown auth failure", CredentialFailureCode.AUTH_FAILED, False),
    ],
)
async def test_legacy_backend_normalizes_auth_failures(
    message: str,
    expected_code: CredentialFailureCode,
    retryable: bool,
) -> None:
    provider = FakeProvider()
    provider.error = WsAuthError(message)
    backend = LegacyWsCredentialBackend(
        signer=FakeSigner({"has_unb": True}),
        provider=provider,
    )

    with pytest.raises(CredentialBackendError) as caught:
        await backend.acquire(ACCOUNT_ID, force_refresh=True)

    assert caught.value.code is expected_code
    assert caught.value.retryable_hint is retryable
    assert str(caught.value) == expected_code.value


@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_legacy_backend_classifies_transient_http_failures_as_retryable(
    status_code: int,
) -> None:
    provider = FakeProvider()
    provider.error = WsAuthError(
        f"账号 {ACCOUNT_ID} WS Token 请求失败(HTTP {status_code})"
    )
    backend = LegacyWsCredentialBackend(
        signer=FakeSigner({"has_unb": True}),
        provider=provider,
    )

    with pytest.raises(CredentialBackendError) as caught:
        await backend.acquire(ACCOUNT_ID, force_refresh=True)

    assert caught.value.code is CredentialFailureCode.NETWORK_ERROR
    assert caught.value.retryable_hint is True
    assert CredentialPolicy().result_state_for(caught.value) is CredentialResultState.RETRYABLE_FAILURE


async def test_non_json_transient_http_failure_remains_retryable() -> None:
    response = httpx.Response(503, text="<html>temporary upstream failure</html>")
    provider = FakeProvider()
    provider.error = WsAuthError(_safe_token_error(ACCOUNT_ID, response))
    backend = LegacyWsCredentialBackend(
        signer=FakeSigner({"has_unb": True}),
        provider=provider,
    )

    with pytest.raises(CredentialBackendError) as caught:
        await backend.acquire(ACCOUNT_ID, force_refresh=True)

    assert caught.value.code is CredentialFailureCode.NETWORK_ERROR
    assert caught.value.retryable_hint is True
    assert CredentialPolicy().result_state_for(caught.value) is CredentialResultState.RETRYABLE_FAILURE


async def test_non_transient_http_auth_failure_stays_terminal() -> None:
    provider = FakeProvider()
    provider.error = WsAuthError(f"账号 {ACCOUNT_ID} WS Token 请求失败(HTTP 401)")
    backend = LegacyWsCredentialBackend(
        signer=FakeSigner({"has_unb": True}),
        provider=provider,
    )

    with pytest.raises(CredentialBackendError) as caught:
        await backend.acquire(ACCOUNT_ID, force_refresh=True)

    assert caught.value.code is CredentialFailureCode.AUTH_FAILED
    assert caught.value.retryable_hint is False
    assert CredentialPolicy().result_state_for(caught.value) is CredentialResultState.TERMINAL_FAILURE


async def test_legacy_backend_does_not_propagate_raw_secret_error_text() -> None:
    provider = FakeProvider()
    provider.error = WsAuthError(
        "FAIL_SYS_USER_VALIDATE cookie=secret-cookie token=secret-token"
    )
    backend = LegacyWsCredentialBackend(
        signer=FakeSigner({"has_unb": True}),
        provider=provider,
    )

    with pytest.raises(CredentialBackendError) as caught:
        await backend.acquire(ACCOUNT_ID, force_refresh=True)

    rendered = f"{caught.value!r} {caught.value}"
    assert "secret-cookie" not in rendered
    assert "secret-token" not in rendered


async def test_validation_gate_maps_legacy_risk_without_worker_state() -> None:
    now = datetime.now(UTC)
    circuit = WorkerRiskCircuit(
        account_id=ACCOUNT_ID,
        code="FAIL_SYS_USER_VALIDATE",
        detected_at=now,
        cooldown_until=now + timedelta(minutes=5),
        recovery_required=True,
    )
    opened: list[str] = []
    cleared: list[str] = []

    async def get_circuit(account_id: str) -> WorkerRiskCircuit | None:
        assert account_id == ACCOUNT_ID
        return circuit

    async def open_circuit(account_id: str) -> WorkerRiskCircuit | None:
        opened.append(account_id)
        return circuit

    async def clear_circuit(account_id: str) -> bool:
        cleared.append(account_id)
        return True

    gate = LegacyValidationGate(
        get_circuit=get_circuit,
        open_circuit=open_circuit,
        clear_circuit=clear_circuit,
    )

    status = await gate.inspect(ACCOUNT_ID)
    await gate.mark_needs_validation(ACCOUNT_ID)
    did_clear = await gate.clear_after_refresh(ACCOUNT_ID)

    assert status.required is True
    assert status.cooling is True
    assert status.code == CredentialFailureCode.NEEDS_VALIDATION.value
    assert opened == [ACCOUNT_ID]
    assert cleared == [ACCOUNT_ID]
    assert did_clear is True
