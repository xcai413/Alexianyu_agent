"""外部发卡 API 配置的应用层(Issue #5 Phase 6, §18)。

配置里只有结构与 secret_ref;密钥值由运营写到 SecretVault,命令行只接收引用名。校验交给
domain 的 ApiDeliveryConfig:https、方法白名单、模板变量白名单、响应路径格式都在那里。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from xianyu_agent.db import get_async_session
from xianyu_agent.domain.delivery import ApiConfigError, ApiDeliveryConfig
from xianyu_agent.domain.delivery.api_config import BUSINESS_VARIABLES
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyApiConfigRepository
from xianyu_agent.infrastructure.delivery import ApiDeliveryProvider, ApiFetchResult

# 试调用用的样例变量:只用于验证"这个配置能不能取到卡",不会碰任何真实订单。
TEST_VARIABLES: dict[str, str] = {
    "order_id": "TEST-ORDER",
    "buyer_name": "测试买家",
    "item_title": "测试商品",
    "sku_name": "",
    "quantity": "1",
}


class ApiConfigService:
    """Create and maintain external card-issuing API configurations."""

    async def create(
        self,
        *,
        name: str,
        url: str,
        method: str = "POST",
        headers: dict[str, str] | None = None,
        body: str = "{}",
        response_path: str = "card",
        timeout_seconds: float = 10.0,
        enabled: bool = True,
        description: str | None = None,
    ) -> ApiDeliveryConfig:
        config = ApiDeliveryConfig(
            name=name,
            url=url,
            method=method,
            headers=dict(headers or {}),
            body=body,
            response_path=response_path,
            timeout_seconds=timeout_seconds,
            enabled=enabled,
            description=description,
        )
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyApiConfigRepository(session)
            existing = await repo.get_by_name(config.name)
            if existing is not None:
                msg = f"API 配置名 {config.name} 已存在"
                raise ApiConfigError(msg)
            config_id = await repo.add(config)
            stored = await repo.get(config_id)
        if stored is None:  # pragma: no cover - 刚写入就消失只可能是并发删除
            msg = f"API 配置 #{config_id} 创建后读取失败"
            raise RuntimeError(msg)
        return stored

    async def get(self, config_id: int) -> ApiDeliveryConfig:
        async with get_async_session() as session:
            config = await SqlAlchemyApiConfigRepository(session).get(config_id)
        if config is None:
            msg = f"API 配置 #{config_id} 不存在"
            raise ValueError(msg)
        return config

    async def list_configs(self, *, only_enabled: bool = False) -> list[ApiDeliveryConfig]:
        async with get_async_session() as session:
            return await SqlAlchemyApiConfigRepository(session).list_configs(
                only_enabled=only_enabled
            )

    async def resolve_ref(self, ref: str) -> ApiDeliveryConfig:
        """库存池的 api_config_ref 可以是配置 id,也可以是配置名。"""
        text = ref.strip()
        if not text:
            msg = "api_config_ref 不能为空"
            raise ValueError(msg)
        async with get_async_session() as session:
            repo = SqlAlchemyApiConfigRepository(session)
            if text.isdigit():
                config = await repo.get(int(text))
                if config is not None:
                    return config
            named = await repo.get_by_name(text)
        if named is None:
            msg = f"找不到发货 API 配置 {text!r}"
            raise ValueError(msg)
        return named

    async def update(
        self,
        config_id: int,
        *,
        name: str | None = None,
        url: str | None = None,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        response_path: str | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
        description: str | None = None,
    ) -> ApiDeliveryConfig:
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyApiConfigRepository(session)
            current = await repo.get(config_id)
            if current is None:
                msg = f"API 配置 #{config_id} 不存在"
                raise ValueError(msg)
            merged = replace(
                current,
                name=name if name is not None else current.name,
                url=url if url is not None else current.url,
                method=method if method is not None else current.method,
                headers=dict(headers) if headers is not None else dict(current.headers),
                body=body if body is not None else current.body,
                response_path=(
                    response_path if response_path is not None else current.response_path
                ),
                timeout_seconds=(
                    timeout_seconds if timeout_seconds is not None else current.timeout_seconds
                ),
                enabled=enabled if enabled is not None else current.enabled,
                description=description if description is not None else current.description,
            )
            validated = ApiDeliveryConfig(
                name=merged.name,
                url=merged.url,
                method=merged.method,
                headers=merged.headers,
                body=merged.body,
                response_path=merged.response_path,
                timeout_seconds=merged.timeout_seconds,
                enabled=merged.enabled,
                description=merged.description,
                local_id=merged.local_id,
            )
            await repo.apply(validated)
        return validated

    async def set_enabled(self, config_id: int, *, enabled: bool) -> ApiDeliveryConfig:
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyApiConfigRepository(session)
            await repo.set_enabled(config_id, enabled=enabled)
            config = await repo.get(config_id)
        if config is None:  # pragma: no cover - 与存在性检查重复
            msg = f"API 配置 #{config_id} 不存在"
            raise ValueError(msg)
        return config

    async def test_fetch(
        self,
        ref: str,
        *,
        variables: Mapping[str, str] | None = None,
        provider: ApiDeliveryProvider | None = None,
    ) -> ApiFetchResult:
        """调用一次发卡 API 验证配置,不预留库存、不给买家发任何消息。

        取到的卡密属于真实资源:调用方只能展示掩码预览,不得打印明文。
        """
        config = await self.resolve_ref(ref)
        merged = {
            **TEST_VARIABLES,
            **{
                key: value
                for key, value in (variables or {}).items()
                if key in BUSINESS_VARIABLES
            },
        }
        fetcher = provider or ApiDeliveryProvider.for_config(config)
        return await fetcher.fetch(config, merged)

    async def delete(self, config_id: int) -> None:
        async with get_async_session() as session, session.begin():
            await SqlAlchemyApiConfigRepository(session).delete(config_id)
