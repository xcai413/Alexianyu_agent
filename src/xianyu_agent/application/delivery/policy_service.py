"""发货策略的应用层编排(Issue #5 Phase 6, §9)。

策略指向库存池,范围字段决定层级。这里做三件事:保存前把配置校验干净(含模板语法与
delivery_mode 和库存池类型是否一致)、解析时给出可审计的命中理由、删除库存池前拦住还被
策略引用的池——静默删掉别人正在用的配置比报错糟得多。
"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.db import get_async_session
from xianyu_agent.domain.delivery import (
    DeliveryPolicy,
    PolicyError,
    PolicyResolution,
    resolve_policy,
)
from xianyu_agent.infrastructure.database.repositories import (
    SqlAlchemyDeliveryPolicyRepository,
    SqlAlchemyInventoryRepository,
)


class DeliveryPolicyService:
    """Create, maintain and resolve delivery policies."""

    async def create(
        self,
        *,
        name: str,
        inventory_pool_id: int,
        account_id: str | None = None,
        item_id: str | None = None,
        sku_id: str | None = None,
        delay_seconds: int = 0,
        delivery_mode: str | None = None,
        template: str | None = None,
        template_ref: str | None = None,
        platform_confirm: bool = False,
        priority: int = 0,
        enabled: bool = True,
        description: str | None = None,
    ) -> DeliveryPolicy:
        policy = DeliveryPolicy(
            name=name,
            inventory_pool_id=inventory_pool_id,
            account_id=account_id,
            item_id=item_id,
            sku_id=sku_id,
            delay_seconds=delay_seconds,
            delivery_mode=delivery_mode,
            template=template,
            template_ref=template_ref,
            platform_confirm=platform_confirm,
            priority=priority,
            enabled=enabled,
            description=description,
        )
        async with get_async_session() as session:
            await self._check_pool(session, policy)
            repo = SqlAlchemyDeliveryPolicyRepository(session)
            policy_id = await repo.add(policy)
            await session.commit()
            created = await repo.get(policy_id)
        if created is None:  # pragma: no cover - 刚写入就消失只可能是并发删除
            msg = f"发货策略 #{policy_id} 创建后读取失败"
            raise RuntimeError(msg)
        return created

    async def get(self, policy_id: int) -> DeliveryPolicy:
        async with get_async_session() as session:
            policy = await SqlAlchemyDeliveryPolicyRepository(session).get(policy_id)
        if policy is None:
            msg = f"发货策略 #{policy_id} 不存在"
            raise ValueError(msg)
        return policy

    async def list(
        self,
        *,
        only_enabled: bool = False,
        account_id: str | None = None,
        item_id: str | None = None,
    ) -> list[DeliveryPolicy]:
        async with get_async_session() as session:
            return await SqlAlchemyDeliveryPolicyRepository(session).list_policies(
                only_enabled=only_enabled, account_id=account_id, item_id=item_id
            )

    async def update(
        self,
        policy_id: int,
        *,
        name: str | None = None,
        inventory_pool_id: int | None = None,
        account_id: str | None = None,
        item_id: str | None = None,
        sku_id: str | None = None,
        delay_seconds: int | None = None,
        delivery_mode: str | None = None,
        template: str | None = None,
        template_ref: str | None = None,
        platform_confirm: bool | None = None,
        priority: int | None = None,
        enabled: bool | None = None,
        description: str | None = None,
        clear_scope: bool = False,
    ) -> DeliveryPolicy:
        """Merge changes onto the stored policy, re-validate, then write back."""
        async with get_async_session() as session:
            repo = SqlAlchemyDeliveryPolicyRepository(session)
            current = await repo.get(policy_id)
            if current is None:
                msg = f"发货策略 #{policy_id} 不存在"
                raise ValueError(msg)
            merged = replace(
                current,
                name=name if name is not None else current.name,
                inventory_pool_id=(
                    inventory_pool_id
                    if inventory_pool_id is not None
                    else current.inventory_pool_id
                ),
                account_id=(
                    None
                    if clear_scope
                    else (account_id if account_id is not None else current.account_id)
                ),
                item_id=None
                if clear_scope
                else (item_id if item_id is not None else current.item_id),
                sku_id=None if clear_scope else (sku_id if sku_id is not None else current.sku_id),
                delay_seconds=(
                    delay_seconds if delay_seconds is not None else current.delay_seconds
                ),
                delivery_mode=(
                    delivery_mode if delivery_mode is not None else current.delivery_mode
                ),
                template=template if template is not None else current.template,
                template_ref=(
                    template_ref if template_ref is not None else current.template_ref
                ),
                platform_confirm=(
                    platform_confirm if platform_confirm is not None else current.platform_confirm
                ),
                priority=priority if priority is not None else current.priority,
                enabled=enabled if enabled is not None else current.enabled,
                description=description if description is not None else current.description,
            )
            validated = DeliveryPolicy(
                name=merged.name,
                inventory_pool_id=merged.inventory_pool_id,
                account_id=merged.account_id,
                item_id=merged.item_id,
                sku_id=merged.sku_id,
                delay_seconds=merged.delay_seconds,
                delivery_mode=merged.delivery_mode,
                template=merged.template,
                template_ref=merged.template_ref,
                platform_confirm=merged.platform_confirm,
                priority=merged.priority,
                enabled=merged.enabled,
                description=merged.description,
                local_id=merged.local_id,
            )
            await self._check_pool(session, validated)
            await repo.replace(validated)
            await session.commit()
        return validated

    async def set_enabled(self, policy_id: int, *, enabled: bool) -> DeliveryPolicy:
        async with get_async_session() as session:
            repo = SqlAlchemyDeliveryPolicyRepository(session)
            await repo.set_enabled(policy_id, enabled=enabled)
            await session.commit()
            policy = await repo.get(policy_id)
        if policy is None:  # pragma: no cover - 与 set_enabled 的存在性检查重复
            msg = f"发货策略 #{policy_id} 不存在"
            raise ValueError(msg)
        return policy

    async def delete(self, policy_id: int) -> None:
        async with get_async_session() as session:
            await SqlAlchemyDeliveryPolicyRepository(session).delete(policy_id)
            await session.commit()

    async def resolve(
        self,
        *,
        account_id: str | None = None,
        item_id: str | None = None,
        sku_id: str | None = None,
    ) -> PolicyResolution:
        """Pick the winning policy and explain it; also checks the target pool."""
        async with get_async_session() as session:
            policies = await SqlAlchemyDeliveryPolicyRepository(session).list_for_resolution(
                account_id=account_id, item_id=item_id, sku_id=sku_id
            )
            resolution = resolve_policy(
                policies, account_id=account_id, item_id=item_id, sku_id=sku_id
            )
            policy = resolution.policy
            if policy is None:
                return resolution
            pool = await SqlAlchemyInventoryRepository(session).get_pool(policy.inventory_pool_id)
        if pool is None:
            return PolicyResolution(
                policy=None,
                level=resolution.level,
                reason=f"命中策略 {policy.describe()},但它指向的库存池不存在",
                candidates=resolution.candidates,
            )
        if not pool.enabled:
            return PolicyResolution(
                policy=None,
                level=resolution.level,
                reason=f"命中策略 {policy.describe()},但库存池 #{pool.local_id} 已停用",
                candidates=resolution.candidates,
            )
        return PolicyResolution(
            policy=policy,
            level=resolution.level,
            reason=f"{resolution.reason},库存池 {pool.describe()}",
            candidates=resolution.candidates,
        )

    @staticmethod
    async def _check_pool(session: AsyncSession, policy: DeliveryPolicy) -> None:
        pool = await SqlAlchemyInventoryRepository(session).get_pool(policy.inventory_pool_id)
        if pool is None:
            msg = f"库存池 #{policy.inventory_pool_id} 不存在"
            raise PolicyError(msg)
        if policy.delivery_mode is not None and policy.delivery_mode != pool.type:
            msg = (
                f"策略的 delivery_mode={policy.delivery_mode} 与库存池 #{pool.local_id} "
                f"的类型 {pool.type} 不一致"
            )
            raise PolicyError(msg)
