"""库存池的应用层命令(Issue #5 Phase 6, 6B)。

领域对象负责校验,仓储负责落库,这一层只做编排与事务边界:每个方法自己开 session、成功后
提交,异常时交给关闭回滚。命令式入口(CLI / REST / MCP)只调用这里,不直接碰仓储。
"""

from __future__ import annotations

from dataclasses import replace

from xianyu_agent.db import get_async_session
from xianyu_agent.domain.inventory import InventoryPool
from xianyu_agent.infrastructure.database.repositories import (
    SqlAlchemyDeliveryPolicyRepository,
    SqlAlchemyInventoryRepository,
)


class InventoryService:
    """Create and maintain inventory pools."""

    async def create_pool(
        self,
        *,
        name: str,
        type_: str,
        description: str | None = None,
        enabled: bool = True,
        delay_seconds: int = 0,
        account_scope: str | None = None,
        item_scope: str | None = None,
        sku_scope: str | None = None,
        api_config_ref: str | None = None,
        content: str | None = None,
        image_url: str | None = None,
    ) -> InventoryPool:
        """Validate and persist a new pool; returns the stored pool with its id."""
        pool = InventoryPool(
            name=name,
            type=type_,
            description=description,
            enabled=enabled,
            delay_seconds=delay_seconds,
            account_scope=account_scope,
            item_scope=item_scope,
            sku_scope=sku_scope,
            api_config_ref=api_config_ref,
            content=content,
            image_url=image_url,
        )
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            pool_id = await repo.create_pool(pool)
            await session.commit()
            created = await repo.get_pool(pool_id)
        if created is None:  # pragma: no cover - 刚写入就消失只可能是并发删除
            msg = f"库存池 #{pool_id} 创建后读取失败"
            raise RuntimeError(msg)
        return created

    async def load(self, pool_id: int) -> InventoryPool:
        async with get_async_session() as session:
            pool = await SqlAlchemyInventoryRepository(session).get_pool(pool_id)
        if pool is None:
            msg = f"库存池 #{pool_id} 不存在"
            raise ValueError(msg)
        return pool

    async def update_pool(
        self,
        pool_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        delay_seconds: int | None = None,
        enabled: bool | None = None,
        content: str | None = None,
        image_url: str | None = None,
        account_scope: str | None = None,
        item_scope: str | None = None,
        sku_scope: str | None = None,
        clear_scope: bool = False,
    ) -> InventoryPool:
        """Merge changes onto the stored pool, re-validate, then write back."""
        async with get_async_session() as session:
            repo = SqlAlchemyInventoryRepository(session)
            current = await repo.get_pool(pool_id)
            if current is None:
                msg = f"库存池 #{pool_id} 不存在"
                raise ValueError(msg)
            merged = replace(
                current,
                name=name if name is not None else current.name,
                description=description if description is not None else current.description,
                delay_seconds=(
                    delay_seconds if delay_seconds is not None else current.delay_seconds
                ),
                enabled=enabled if enabled is not None else current.enabled,
                account_scope=(
                    None
                    if clear_scope
                    else account_scope if account_scope is not None else current.account_scope
                ),
                item_scope=(
                    None if clear_scope else item_scope if item_scope is not None else current.item_scope
                ),
                sku_scope=(
                    None if clear_scope else sku_scope if sku_scope is not None else current.sku_scope
                ),
                content=content if content is not None else current.content,
                image_url=image_url if image_url is not None else current.image_url,
            )
            # replace 不重新执行 __post_init__,校验必须显式做一次。
            validated = InventoryPool(
                name=merged.name,
                type=merged.type,
                description=merged.description,
                enabled=merged.enabled,
                delay_seconds=merged.delay_seconds,
                account_scope=merged.account_scope,
                item_scope=merged.item_scope,
                sku_scope=merged.sku_scope,
                api_config_ref=merged.api_config_ref,
                content=merged.content,
                image_url=merged.image_url,
                local_id=merged.local_id,
            )
            await repo.apply_pool(validated)
            await session.commit()
        return validated

    async def set_enabled(self, pool_id: int, *, enabled: bool) -> InventoryPool:
        """切换启用状态;启用前重新校验一次内容完整度。"""
        # 走 update_pool 的合并 + 校验路径:直接 UPDATE enabled 会绕过领域校验,
        # 让一个没有文案的 text 池变成"已启用但发不出内容"。
        return await self.update_pool(pool_id, enabled=enabled)

    async def delete_pool(self, pool_id: int) -> None:
        """删除池及其单元与预留记录;仍被发货策略引用时拒绝删除。"""
        async with get_async_session() as session:
            referencing = await SqlAlchemyDeliveryPolicyRepository(session).count_for_pool(pool_id)
            if referencing:
                msg = (
                    f"库存池 #{pool_id} 还被 {referencing} 条发货策略引用,请先删除或改指向这些策略"
                )
                raise ValueError(msg)
            await SqlAlchemyInventoryRepository(session).delete_pool(pool_id)
            await session.commit()
