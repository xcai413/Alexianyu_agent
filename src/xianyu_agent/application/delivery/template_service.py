"""可复用发货模板库的应用层(参考 Helper 的发货模板)。

模板本身只是文案:合法性由 domain 的 DeliveryTemplate 校验(白名单变量)。这里只负责
创建、改名、替换内容、启停与删除,并在删除前提醒是否仍被发货策略引用。
"""

from __future__ import annotations

from dataclasses import replace

from xianyu_agent.db import get_async_session
from xianyu_agent.domain.delivery import DeliveryTemplate, TemplateError
from xianyu_agent.infrastructure.database.repositories import (
    SqlAlchemyDeliveryPolicyRepository,
    SqlAlchemyDeliveryTemplateRepository,
)


class DeliveryTemplateService:
    """Create and maintain reusable delivery templates."""

    async def create(
        self,
        *,
        name: str,
        content: str,
        description: str | None = None,
        enabled: bool = True,
    ) -> DeliveryTemplate:
        template = DeliveryTemplate(
            name=name, content=content, description=description, enabled=enabled
        )
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryTemplateRepository(session)
            if await repo.get_by_name(template.name) is not None:
                msg = f"发货模板 {template.name} 已存在"
                raise TemplateError(msg)
            template_id = await repo.add(template)
            stored = await repo.get(template_id)
        if stored is None:  # pragma: no cover - 刚写入就消失只可能是并发删除
            msg = f"发货模板 #{template_id} 创建后读取失败"
            raise RuntimeError(msg)
        return stored

    async def get(self, template_id: int) -> DeliveryTemplate:
        async with get_async_session() as session:
            template = await SqlAlchemyDeliveryTemplateRepository(session).get(template_id)
        if template is None:
            msg = f"发货模板 #{template_id} 不存在"
            raise ValueError(msg)
        return template

    async def resolve_ref(self, ref: str) -> DeliveryTemplate:
        """发货策略的 template_ref 可以是模板 id,也可以是模板名。"""
        text = ref.strip()
        if not text:
            msg = "template_ref 不能为空"
            raise ValueError(msg)
        async with get_async_session() as session:
            repo = SqlAlchemyDeliveryTemplateRepository(session)
            if text.isdigit():
                template = await repo.get(int(text))
                if template is not None:
                    return template
            named = await repo.get_by_name(text)
        if named is None:
            msg = f"找不到发货模板 {text!r}"
            raise ValueError(msg)
        return named

    async def list_templates(self, *, only_enabled: bool = False) -> list[DeliveryTemplate]:
        async with get_async_session() as session:
            return await SqlAlchemyDeliveryTemplateRepository(session).list_templates(
                only_enabled=only_enabled
            )

    async def update(
        self,
        template_id: int,
        *,
        name: str | None = None,
        content: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> DeliveryTemplate:
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryTemplateRepository(session)
            current = await repo.get(template_id)
            if current is None:
                msg = f"发货模板 #{template_id} 不存在"
                raise ValueError(msg)
            merged = replace(
                current,
                name=name if name is not None else current.name,
                content=content if content is not None else current.content,
                description=description if description is not None else current.description,
                enabled=enabled if enabled is not None else current.enabled,
            )
            validated = DeliveryTemplate(
                name=merged.name,
                content=merged.content,
                description=merged.description,
                enabled=merged.enabled,
                local_id=merged.local_id,
            )
            await repo.apply(validated)
        return validated

    async def set_enabled(self, template_id: int, *, enabled: bool) -> DeliveryTemplate:
        return await self.update(template_id, enabled=enabled)

    async def usage_count(self, name: str) -> int:
        """有多少条启用的发货策略在引用这个模板。"""
        async with get_async_session() as session:
            repo = SqlAlchemyDeliveryPolicyRepository(session)
            policies = await repo.list_policies(only_enabled=True)
        return sum(1 for policy in policies if policy.template_ref == name)

    async def delete(self, template_id: int) -> int:
        """删除模板并返回仍引用它的启用策略数;有引用时拒绝删除。"""
        template = await self.get(template_id)
        used_by = await self.usage_count(template.name)
        if used_by:
            msg = f"发货模板 {template.name} 仍被 {used_by} 条启用策略引用,先改引用再删除"
            raise ValueError(msg)
        async with get_async_session() as session, session.begin():
            await SqlAlchemyDeliveryTemplateRepository(session).delete(template_id)
        return used_by
