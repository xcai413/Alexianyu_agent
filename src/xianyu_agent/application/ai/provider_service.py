"""AI provider configuration without exposing secret material."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from xianyu_agent.application.ports.secret_vault import (
    SecretMetadata,
    SecretRef,
    SecretValue,
    SecretVault,
)
from xianyu_agent.db import get_async_session
from xianyu_agent.domain.ai import PROVIDER_TYPES
from xianyu_agent.infrastructure.database.models.ai import AIProviderConfig

OPENAI_COMPATIBLE = "openai_compatible"


@dataclass(frozen=True)
class AIProviderView:
    id: int
    name: str
    provider_type: str
    base_url: str
    default_model: str
    enabled: bool
    timeout_seconds: int
    max_retries: int
    configured: bool


@dataclass(frozen=True)
class ResolvedAIProvider:
    provider_type: str
    base_url: str
    default_model: str
    timeout_seconds: int
    max_retries: int
    api_key: SecretValue
    extra_config: dict[str, Any] = field(default_factory=dict)


class AIProviderService:
    def __init__(self, vault: SecretVault) -> None:
        self._vault = vault

    async def create(
        self,
        *,
        provider_type: str,
        name: str,
        base_url: str,
        default_model: str,
        api_key: SecretValue | None = None,
        enabled: bool = False,
        timeout_seconds: int = 15,
        max_retries: int = 0,
        extra_config: dict[str, Any] | None = None,
    ) -> AIProviderView:
        """Create one provider; the type must be one of the declared provider types."""
        kind = provider_type.strip().lower()
        if kind not in PROVIDER_TYPES:
            raise ValueError(f"未知 AI Provider 类型: {provider_type}")
        if not name.strip() or not default_model.strip():
            raise ValueError("name 和 default_model 不能为空")
        if timeout_seconds <= 0 or max_retries < 0:
            raise ValueError("timeout_seconds 必须为正数且 max_retries 不能为负数")
        secret_ref = None
        if api_key is not None:
            secret_ref = await self._vault.put(
                api_key,
                metadata=SecretMetadata({"kind": "ai_provider", "provider_type": kind}),
            )
        async with get_async_session() as session:
            row = AIProviderConfig(
                name=name.strip(),
                provider_type=kind,
                base_url=base_url.strip(),
                secret_ref=secret_ref.value if secret_ref else None,
                default_model=default_model.strip(),
                enabled=enabled,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
                extra_config=dict(extra_config or {}),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return await self._view(row)

    async def list(self) -> list[AIProviderView]:
        async with get_async_session() as session:
            rows = list(
                (
                    await session.execute(select(AIProviderConfig).order_by(AIProviderConfig.id))
                ).scalars()
            )
        return [await self._view(row) for row in rows]

    async def set_enabled(self, name: str, enabled: bool) -> AIProviderView:
        async with get_async_session() as session:
            row = await session.scalar(
                select(AIProviderConfig).where(AIProviderConfig.name == name)
            )
            if row is None:
                raise ValueError(f"Provider {name} 不存在")
            row.enabled = enabled
            await session.commit()
            await session.refresh(row)
        return await self._view(row)

    async def set_max_retries(self, name: str, max_retries: int) -> AIProviderView:
        if not 0 <= max_retries <= 3:
            raise ValueError("max_retries 必须在 0 到 3 之间")
        async with get_async_session() as session:
            row = await session.scalar(
                select(AIProviderConfig).where(AIProviderConfig.name == name)
            )
            if row is None:
                raise ValueError(f"Provider {name} 不存在")
            row.max_retries = max_retries
            await session.commit()
            await session.refresh(row)
        return await self._view(row)

    async def replace_api_key(self, name: str, api_key: SecretValue) -> AIProviderView:
        async with get_async_session() as session:
            row = await session.scalar(
                select(AIProviderConfig).where(AIProviderConfig.name == name)
            )
            if row is None:
                raise ValueError(f"Provider {name} 不存在")
            if row.secret_ref is None:
                ref = await self._vault.put(
                    api_key,
                    metadata=SecretMetadata(
                        {"kind": "ai_provider", "provider_type": row.provider_type}
                    ),
                )
                row.secret_ref = ref.value
            else:
                await self._vault.replace(SecretRef(row.secret_ref), api_key)
            await session.commit()
            await session.refresh(row)
        return await self._view(row)

    async def resolve_enabled(self, provider_type: str | None = None) -> ResolvedAIProvider:
        """Return the first enabled provider, optionally limited to one explicit type."""
        conditions = [AIProviderConfig.enabled.is_(True)]
        if provider_type is not None:
            conditions.append(AIProviderConfig.provider_type == provider_type)
        async with get_async_session() as session:
            row = (
                await session.execute(
                    select(AIProviderConfig)
                    .where(*conditions)
                    .order_by(AIProviderConfig.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is None or row.secret_ref is None:
            raise ValueError("未配置已启用的 AI Provider")
        return ResolvedAIProvider(
            provider_type=row.provider_type,
            base_url=row.base_url,
            default_model=row.default_model,
            timeout_seconds=row.timeout_seconds,
            max_retries=row.max_retries,
            api_key=await self._vault.resolve(SecretRef(row.secret_ref)),
            extra_config=dict(row.extra_config or {}),
        )

    async def _view(self, row: AIProviderConfig) -> AIProviderView:
        configured = row.secret_ref is not None and await self._vault.configured(
            SecretRef(row.secret_ref)
        )
        return AIProviderView(
            id=row.id,
            name=row.name,
            provider_type=row.provider_type,
            base_url=row.base_url,
            default_model=row.default_model,
            enabled=row.enabled,
            timeout_seconds=row.timeout_seconds,
            max_retries=row.max_retries,
            configured=configured,
        )
