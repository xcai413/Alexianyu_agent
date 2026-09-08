"""Compatibility adapters for the legacy WS credential and validation primitives."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, cast

from xianyu_agent.application.session.ports import (
    CredentialBackendError,
    CredentialBackendStatus,
    CredentialFailureCode,
    ValidationStatus,
)
from xianyu_agent.domain.account import risk as worker_risk
from xianyu_agent.domain.account.risk import WorkerRiskCircuit
from xianyu_agent.protocol.signer import CookieSigner
from xianyu_agent.protocol.ws_auth import (
    WsAuthError,
    WsCredentials,
    WsCredentialStatus,
    WsTokenProvider,
)

_TRANSIENT_HTTP_ERROR = re.compile(r"\bHTTP\s+(?:500|502|503)\b")


class _SignerLike(Protocol):
    async def fingerprint(self, account_id: str) -> Mapping[str, object] | None: ...


class _ProviderLike(Protocol):
    async def status(self, account_id: str) -> WsCredentialStatus: ...

    async def get_credentials(
        self,
        account_id: str,
        *,
        force_refresh: bool = False,
    ) -> WsCredentials: ...


RiskGetter = Callable[[str], Awaitable[WorkerRiskCircuit | None]]
RiskMarker = Callable[[str], Awaitable[WorkerRiskCircuit | None]]
RiskClearer = Callable[[str], Awaitable[bool]]


class LegacyWsCredentialBackend:
    """Expose ``WsTokenProvider`` through the application credential port."""

    def __init__(
        self,
        *,
        signer: _SignerLike | None = None,
        provider: _ProviderLike | None = None,
    ) -> None:
        concrete_signer = signer or CookieSigner()
        self._signer = concrete_signer
        self._provider = provider or WsTokenProvider(signer=cast(CookieSigner, concrete_signer))

    async def inspect(self, account_id: str) -> CredentialBackendStatus:
        fingerprint = await self._signer.fingerprint(account_id)
        status = await self._provider.status(account_id)
        return CredentialBackendStatus(
            account_id=account_id,
            cookie_available=fingerprint is not None,
            identity_available=bool(fingerprint and fingerprint.get("has_unb")),
            token_cached=status.token_cached,
            expires_at=status.expires_at,
        )

    async def acquire(
        self,
        account_id: str,
        *,
        force_refresh: bool = False,
    ) -> WsCredentials:
        try:
            return await self._provider.get_credentials(
                account_id,
                force_refresh=force_refresh,
            )
        except WsAuthError as exc:
            raise _translate_auth_error(exc) from None


class LegacyValidationGate:
    """Adapt the durable worker-risk circuit without importing WorkerState."""

    def __init__(
        self,
        *,
        get_circuit: RiskGetter = worker_risk.get,
        open_circuit: RiskMarker = worker_risk.open_user_validate,
        clear_circuit: RiskClearer = worker_risk.clear_after_refresh,
    ) -> None:
        self._get_circuit = get_circuit
        self._open_circuit = open_circuit
        self._clear_circuit = clear_circuit

    async def inspect(self, account_id: str) -> ValidationStatus:
        circuit = await self._get_circuit(account_id)
        if circuit is None:
            return ValidationStatus(account_id=account_id, required=False)
        return ValidationStatus(
            account_id=account_id,
            required=circuit.recovery_required,
            cooling=circuit.is_cooling(),
            code=CredentialFailureCode.NEEDS_VALIDATION.value,
        )

    async def mark_needs_validation(self, account_id: str) -> None:
        await self._open_circuit(account_id)

    async def clear_after_refresh(self, account_id: str) -> bool:
        return await self._clear_circuit(account_id)


def _translate_auth_error(error: WsAuthError) -> CredentialBackendError:
    text = str(error)
    upper = text.upper()
    compact = upper.replace(" ", "")
    retryable = False

    if worker_risk.is_user_validate_error(error):
        code = CredentialFailureCode.NEEDS_VALIDATION
    elif (
        "网络请求失败" in text
        or "NETWORK" in upper
        or _TRANSIENT_HTTP_ERROR.search(upper) is not None
    ):
        code = CredentialFailureCode.NETWORK_ERROR
        retryable = True
    elif "无可用COOKIE" in compact:
        code = CredentialFailureCode.CREDENTIAL_MISSING
    elif "COOKIE缺少UNB" in compact:
        code = CredentialFailureCode.IDENTITY_MISSING
    elif "SESSION_EXPIRED" in upper or "SESSION过期" in compact:
        code = CredentialFailureCode.SESSION_EXPIRED
    elif "账号" in text and "不存在" in text:
        code = CredentialFailureCode.ACCOUNT_NOT_FOUND
    else:
        code = CredentialFailureCode.AUTH_FAILED

    return CredentialBackendError(code, retryable_hint=retryable)
