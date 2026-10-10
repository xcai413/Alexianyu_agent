"""Resolve the enabled provider from the secret vault at call time."""

from __future__ import annotations

import logging

from xianyu_agent.application.ai import AIProviderService
from xianyu_agent.domain.ai import AIRequest, AIResponse, UnconfiguredError
from xianyu_agent.infrastructure.secrets import FernetKeyring, SqlAlchemySecretVault
from xianyu_agent.infrastructure.secrets.fernet_vault import (
    InvalidMasterKeyError,
    MissingMasterKeyError,
    SecretNotFoundError,
)

from .providers import build_provider

logger = logging.getLogger(__name__)


class VaultBackedProvider:
    """Resolve the enabled Provider lazily; never fall back to environment keys."""

    name = "vault"
    provider_type = "vault"

    def __init__(
        self,
        service: AIProviderService | None = None,
        *,
        provider_type: str | None = None,
    ) -> None:
        self._service = service
        self._provider_type = provider_type

    async def generate(self, request: AIRequest) -> AIResponse:
        try:
            service = self._service or _default_service()
            resolved = await service.resolve_enabled(self._provider_type)
        except (
            InvalidMasterKeyError,
            MissingMasterKeyError,
            SecretNotFoundError,
            UnconfiguredError,
            ValueError,
        ) as exc:
            raise UnconfiguredError("AI Provider 未配置或密钥不可用") from exc
        return await build_provider(resolved.provider_type, resolved).generate(request)


def _default_service() -> AIProviderService:
    return AIProviderService(SqlAlchemySecretVault(FernetKeyring.from_environment()))
