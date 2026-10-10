"""发货策略仓储(Issue #5 Phase 6)。

事务由调用方拥有:仓储只在当前事务里读写,提交与回滚由应用服务决定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.domain.delivery import DeliveryPolicy, DeliveryTemplate, check_template
from xianyu_agent.domain.delivery.api_config import ApiDeliveryConfig
from xianyu_agent.domain.delivery.guard import BlacklistEntry, GuardRule
from xianyu_agent.domain.delivery.job import DeliveryAttempt, DeliveryJob
from xianyu_agent.infrastructure.database.models import (
    Account as AccountRow,
    ApiDeliveryConfigRow,
    DeliveryAttempt as DeliveryAttemptRow,
    DeliveryGuardRule as DeliveryGuardRuleRow,
    DeliveryJob as DeliveryJobRow,
    DeliveryPolicy as DeliveryPolicyRow,
    DeliveryTemplateRow,
    Order as OrderRow,
    PersonalBlacklist as PersonalBlacklistRow,
)


def _to_guard_rule(row: DeliveryGuardRuleRow) -> GuardRule:
    return GuardRule(
        rule_code=row.rule_code,
        enabled=bool(row.enabled),
        priority=int(row.priority),
        block_reason=row.block_reason,
        excluded_item_ids=tuple(row.excluded_item_ids or ()),
        config=dict(row.config or {}),
        account_id=row.account_id,
        local_id=row.id,
    )


def _to_blacklist_entry(row: PersonalBlacklistRow) -> BlacklistEntry:
    return BlacklistEntry(
        buyer_id=row.buyer_id,
        reason=row.reason or "",
        account_id=row.account_id,
        item_id=row.item_id,
        local_id=row.id,
    )


class SqlAlchemyDeliveryGuardRepository:
    """守卫配置、黑名单与判定所需订单计数的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- 规则 ------------------------------------------------------------

    async def upsert_rule(self, rule: GuardRule) -> int:
        """Insert or update one (account, rule_code) pair.

        account_id 为空的全局规则无法用唯一约束去重(NULL 互不相等),所以这里先查后写。
        """
        statement = select(DeliveryGuardRuleRow).where(
            DeliveryGuardRuleRow.rule_code == rule.rule_code
        )
        if rule.account_id is None:
            statement = statement.where(DeliveryGuardRuleRow.account_id.is_(None))
        else:
            statement = statement.where(DeliveryGuardRuleRow.account_id == rule.account_id)
        existing = await self._session.scalar(statement.limit(1))
        if existing is not None:
            existing.enabled = rule.enabled
            existing.priority = rule.priority
            existing.block_reason = rule.block_reason
            existing.excluded_item_ids = list(rule.excluded_item_ids) or None
            existing.config = dict(rule.config) or None
            await self._session.flush()
            return int(existing.id)
        row = DeliveryGuardRuleRow(
            account_id=rule.account_id,
            rule_code=rule.rule_code,
            enabled=rule.enabled,
            priority=rule.priority,
            block_reason=rule.block_reason,
            excluded_item_ids=list(rule.excluded_item_ids) or None,
            config=dict(rule.config) or None,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def get_rule(self, rule_id: int) -> GuardRule | None:
        row = await self._session.get(DeliveryGuardRuleRow, rule_id)
        return None if row is None else _to_guard_rule(row)

    async def list_rules(
        self, *, account_id: str | None = None, only_enabled: bool = False, limit: int = 200
    ) -> list[GuardRule]:
        statement = select(DeliveryGuardRuleRow).order_by(DeliveryGuardRuleRow.id).limit(limit)
        if account_id is not None:
            statement = statement.where(
                or_(
                    DeliveryGuardRuleRow.account_id == account_id,
                    DeliveryGuardRuleRow.account_id.is_(None),
                )
            )
        if only_enabled:
            statement = statement.where(DeliveryGuardRuleRow.enabled.is_(True))
        rows = (await self._session.scalars(statement)).all()
        return [_to_guard_rule(row) for row in rows]

    async def set_rule_enabled(self, rule_id: int, *, enabled: bool) -> None:
        result = await self._session.execute(
            update(DeliveryGuardRuleRow)
            .where(DeliveryGuardRuleRow.id == rule_id)
            .values(enabled=enabled)
        )
        if _rowcount(result) != 1:
            msg = f"守卫规则 #{rule_id} 不存在"
            raise ValueError(msg)

    async def delete_rule(self, rule_id: int) -> None:
        result = await self._session.execute(
            delete(DeliveryGuardRuleRow).where(DeliveryGuardRuleRow.id == rule_id)
        )
        if _rowcount(result) != 1:
            msg = f"守卫规则 #{rule_id} 不存在"
            raise ValueError(msg)

    # ---- 黑名单 ----------------------------------------------------------

    async def add_blacklist(self, entry: BlacklistEntry) -> int:
        row = PersonalBlacklistRow(
            buyer_id=entry.buyer_id,
            account_id=entry.account_id,
            item_id=entry.item_id,
            reason=entry.reason or None,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def list_blacklist(
        self,
        *,
        buyer_id: str | None = None,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[BlacklistEntry]:
        statement = select(PersonalBlacklistRow).order_by(PersonalBlacklistRow.id).limit(limit)
        if buyer_id is not None:
            statement = statement.where(PersonalBlacklistRow.buyer_id == buyer_id)
        if account_id is not None:
            statement = statement.where(
                or_(
                    PersonalBlacklistRow.account_id == account_id,
                    PersonalBlacklistRow.account_id.is_(None),
                )
            )
        rows = (await self._session.scalars(statement)).all()
        return [_to_blacklist_entry(row) for row in rows]

    async def delete_blacklist(self, entry_id: int) -> None:
        result = await self._session.execute(
            delete(PersonalBlacklistRow).where(PersonalBlacklistRow.id == entry_id)
        )
        if _rowcount(result) != 1:
            msg = f"黑名单 #{entry_id} 不存在"
            raise ValueError(msg)

    # ---- 判定所需的订单计数 ----------------------------------------------

    async def count_buyer_active_orders(
        self, *, account_id: str, buyer_id: str, statuses: tuple[str, ...]
    ) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(OrderRow)
            .join(AccountRow, AccountRow.id == OrderRow.account_id)
            .where(
                AccountRow.account_id == account_id,
                OrderRow.buyer_id == buyer_id,
                OrderRow.status.in_(statuses),
            )
        )
        return int(total or 0)

    async def count_buyer_active_orders_global(
        self, *, buyer_id: str, statuses: tuple[str, ...]
    ) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(OrderRow)
            .where(OrderRow.buyer_id == buyer_id, OrderRow.status.in_(statuses))
        )
        return int(total or 0)

    async def count_buyer_unconfirmed_orders(
        self, *, buyer_id: str, statuses: tuple[str, ...]
    ) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(OrderRow)
            .where(OrderRow.buyer_id == buyer_id, OrderRow.status.in_(statuses))
        )
        return int(total or 0)


DEFAULT_LIST_LIMIT = 200


def _rowcount(result: object) -> int:
    return int(cast(Any, result).rowcount or 0)


def _scope_clause(column: Any, value: str | None) -> Any:
    """Rows whose scope is either unset or equal to the requested value."""
    if value is None:
        return column.is_(None)
    return or_(column.is_(None), column == value)


def _to_policy(row: DeliveryPolicyRow) -> DeliveryPolicy:
    return DeliveryPolicy(
        name=row.name,
        inventory_pool_id=int(row.inventory_pool_id),
        account_id=row.account_id,
        item_id=row.item_id,
        sku_id=row.sku_id,
        delay_seconds=int(row.delay_seconds),
        delivery_mode=row.delivery_mode,
        template=row.template,
        template_ref=row.template_ref,
        platform_confirm=bool(row.platform_confirm),
        priority=int(row.priority),
        enabled=bool(row.enabled),
        description=row.description,
        local_id=row.id,
        template_variables=() if row.template is None else check_template(row.template),
    )


def _to_template(row: DeliveryTemplateRow) -> DeliveryTemplate:
    return DeliveryTemplate(
        name=row.name,
        content=row.content,
        description=row.description,
        enabled=bool(row.enabled),
        local_id=row.id,
    )


class SqlAlchemyDeliveryTemplateRepository:
    """可复用发货模板库的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, template: DeliveryTemplate) -> int:
        row = DeliveryTemplateRow(
            name=template.name,
            content=template.content,
            description=template.description,
            enabled=template.enabled,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def get(self, template_id: int) -> DeliveryTemplate | None:
        row = await self._session.get(DeliveryTemplateRow, template_id)
        return None if row is None else _to_template(row)

    async def get_by_name(self, name: str) -> DeliveryTemplate | None:
        row = await self._session.scalar(
            select(DeliveryTemplateRow).where(DeliveryTemplateRow.name == name).limit(1)
        )
        return None if row is None else _to_template(row)

    async def list_templates(
        self, *, only_enabled: bool = False, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[DeliveryTemplate]:
        statement = select(DeliveryTemplateRow).order_by(DeliveryTemplateRow.id).limit(limit)
        if only_enabled:
            statement = statement.where(DeliveryTemplateRow.enabled.is_(True))
        return [_to_template(row) for row in (await self._session.scalars(statement)).all()]

    async def apply(self, template: DeliveryTemplate) -> None:
        if template.local_id is None:
            msg = "更新发货模板需要 local_id"
            raise ValueError(msg)
        result = await self._session.execute(
            update(DeliveryTemplateRow)
            .where(DeliveryTemplateRow.id == template.local_id)
            .values(
                name=template.name,
                content=template.content,
                description=template.description,
                enabled=template.enabled,
            )
        )
        if _rowcount(result) != 1:
            msg = f"发货模板 #{template.local_id} 不存在"
            raise ValueError(msg)

    async def delete(self, template_id: int) -> None:
        await self._session.execute(
            delete(DeliveryTemplateRow).where(DeliveryTemplateRow.id == template_id)
        )


class SqlAlchemyDeliveryPolicyRepository:
    """Delivery policy repository backed by one caller-owned AsyncSession."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, policy: DeliveryPolicy) -> int:
        row = DeliveryPolicyRow(
            name=policy.name,
            inventory_pool_id=policy.inventory_pool_id,
            account_id=policy.account_id,
            item_id=policy.item_id,
            sku_id=policy.sku_id,
            delay_seconds=policy.delay_seconds,
            delivery_mode=policy.delivery_mode,
            template=policy.template,
            template_ref=policy.template_ref,
            platform_confirm=policy.platform_confirm,
            priority=policy.priority,
            enabled=policy.enabled,
            description=policy.description,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def get(self, policy_id: int) -> DeliveryPolicy | None:
        row = await self._session.get(DeliveryPolicyRow, policy_id)
        return None if row is None else _to_policy(row)

    async def list_policies(
        self,
        *,
        only_enabled: bool = False,
        account_id: str | None = None,
        item_id: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[DeliveryPolicy]:
        statement = select(DeliveryPolicyRow).order_by(DeliveryPolicyRow.id).limit(limit)
        if only_enabled:
            statement = statement.where(DeliveryPolicyRow.enabled.is_(True))
        if account_id is not None:
            statement = statement.where(DeliveryPolicyRow.account_id == account_id)
        if item_id is not None:
            statement = statement.where(DeliveryPolicyRow.item_id == item_id)
        rows = (await self._session.scalars(statement)).all()
        return [_to_policy(row) for row in rows]

    async def list_for_resolution(
        self,
        *,
        account_id: str | None = None,
        item_id: str | None = None,
        sku_id: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[DeliveryPolicy]:
        """Enabled policies whose scope could match this request, plus global ones."""
        statement = (
            select(DeliveryPolicyRow)
            .where(DeliveryPolicyRow.enabled.is_(True))
            .order_by(DeliveryPolicyRow.id)
            .limit(limit)
        )
        clauses: list[Any] = [DeliveryPolicyRow.account_id.is_(None)]
        if account_id is not None:
            clauses.append(DeliveryPolicyRow.account_id == account_id)
        statement = statement.where(or_(*clauses))
        statement = statement.where(_scope_clause(DeliveryPolicyRow.item_id, item_id))
        statement = statement.where(_scope_clause(DeliveryPolicyRow.sku_id, sku_id))
        rows = (await self._session.scalars(statement)).all()
        return [_to_policy(row) for row in rows]

    async def replace(self, policy: DeliveryPolicy) -> None:
        if policy.local_id is None:
            msg = "覆盖策略需要 local_id"
            raise ValueError(msg)
        result = await self._session.execute(
            update(DeliveryPolicyRow)
            .where(DeliveryPolicyRow.id == policy.local_id)
            .values(
                name=policy.name,
                inventory_pool_id=policy.inventory_pool_id,
                account_id=policy.account_id,
                item_id=policy.item_id,
                sku_id=policy.sku_id,
                delay_seconds=policy.delay_seconds,
                delivery_mode=policy.delivery_mode,
                template=policy.template,
                template_ref=policy.template_ref,
                platform_confirm=policy.platform_confirm,
                priority=policy.priority,
                enabled=policy.enabled,
                description=policy.description,
            )
        )
        if _rowcount(result) != 1:
            msg = f"发货策略 #{policy.local_id} 不存在"
            raise ValueError(msg)

    async def set_enabled(self, policy_id: int, *, enabled: bool) -> None:
        result = await self._session.execute(
            update(DeliveryPolicyRow)
            .where(DeliveryPolicyRow.id == policy_id)
            .values(enabled=enabled)
        )
        if _rowcount(result) != 1:
            msg = f"发货策略 #{policy_id} 不存在"
            raise ValueError(msg)

    async def delete(self, policy_id: int) -> None:
        result = await self._session.execute(
            delete(DeliveryPolicyRow).where(DeliveryPolicyRow.id == policy_id)
        )
        if _rowcount(result) != 1:
            msg = f"发货策略 #{policy_id} 不存在"
            raise ValueError(msg)

    async def count_for_pool(self, pool_id: int) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(DeliveryPolicyRow)
            .where(DeliveryPolicyRow.inventory_pool_id == pool_id)
        )
        return int(total or 0)


def _to_job(row: DeliveryJobRow) -> DeliveryJob:
    return DeliveryJob(
        account_id=row.account_id,
        order_id=row.order_id,
        required_quantity=int(row.required_quantity),
        semantic_key=row.semantic_key,
        state=row.state,
        policy_snapshot=dict(row.policy_snapshot or {}),
        reservation_id=row.reservation_id,
        item_id=row.item_id,
        sku_ref=row.sku_ref,
        last_error=row.last_error,
        local_id=row.id,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _to_attempt(row: DeliveryAttemptRow) -> DeliveryAttempt:
    return DeliveryAttempt(
        job_id=row.job_id,
        attempt_no=int(row.attempt_no),
        action=row.action,
        result=row.result,
        external_message_id=row.external_message_id,
        error=row.error,
        local_id=row.id,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class SqlAlchemyDeliveryJobRepository:
    """Delivery job repository backed by one caller-owned AsyncSession."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, job: DeliveryJob) -> int:
        row = DeliveryJobRow(
            account_id=job.account_id,
            order_id=job.order_id,
            semantic_key=job.semantic_key,
            state=job.state,
            required_quantity=job.required_quantity,
            policy_snapshot=job.policy_snapshot or None,
            reservation_id=job.reservation_id,
            item_id=job.item_id,
            sku_ref=job.sku_ref,
            last_error=job.last_error,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def get(self, job_id: int) -> DeliveryJob | None:
        row = await self._session.get(DeliveryJobRow, job_id)
        return None if row is None else _to_job(row)

    async def get_by_identity(
        self, account_id: str, order_id: str, semantic_key: str
    ) -> DeliveryJob | None:
        row = await self._session.scalar(
            select(DeliveryJobRow)
            .where(
                DeliveryJobRow.account_id == account_id,
                DeliveryJobRow.order_id == order_id,
                DeliveryJobRow.semantic_key == semantic_key,
            )
            .limit(1)
        )
        return None if row is None else _to_job(row)

    async def list_jobs(
        self,
        *,
        account_id: str | None = None,
        order_id: str | None = None,
        state: str | None = None,
        states: tuple[str, ...] | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[DeliveryJob]:
        statement = select(DeliveryJobRow).order_by(DeliveryJobRow.id.desc()).limit(limit)
        if account_id is not None:
            statement = statement.where(DeliveryJobRow.account_id == account_id)
        if order_id is not None:
            statement = statement.where(DeliveryJobRow.order_id == order_id)
        if state is not None:
            statement = statement.where(DeliveryJobRow.state == state)
        if states is not None:
            statement = statement.where(DeliveryJobRow.state.in_(states))
        rows = (await self._session.scalars(statement)).all()
        return [_to_job(row) for row in rows]

    async def apply(self, job: DeliveryJob) -> None:
        """Persist state and the fields a state change can move."""
        if job.local_id is None:
            msg = "更新发货任务需要 local_id"
            raise ValueError(msg)
        result = await self._session.execute(
            update(DeliveryJobRow)
            .where(DeliveryJobRow.id == job.local_id)
            .values(
                state=job.state,
                reservation_id=job.reservation_id,
                last_error=job.last_error,
                started_at=job.started_at,
                completed_at=job.completed_at,
            )
        )
        if _rowcount(result) != 1:
            msg = f"发货任务 #{job.local_id} 不存在"
            raise ValueError(msg)

    async def count_attempts(self, job_id: int) -> int:
        total = await self._session.scalar(
            select(func.count())
            .select_from(DeliveryAttemptRow)
            .where(DeliveryAttemptRow.job_id == job_id)
        )
        return int(total or 0)

    async def add_attempt(self, attempt: DeliveryAttempt) -> int:
        row = DeliveryAttemptRow(
            job_id=attempt.job_id,
            attempt_no=attempt.attempt_no,
            action=attempt.action,
            result=attempt.result,
            external_message_id=attempt.external_message_id,
            error=attempt.error,
            finished_at=attempt.finished_at or datetime.now(UTC),
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def list_attempts(
        self, job_id: int, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[DeliveryAttempt]:
        rows = (
            await self._session.scalars(
                select(DeliveryAttemptRow)
                .where(DeliveryAttemptRow.job_id == job_id)
                .order_by(DeliveryAttemptRow.attempt_no)
                .limit(limit)
            )
        ).all()
        return [_to_attempt(row) for row in rows]


def _to_api_config(row: ApiDeliveryConfigRow) -> ApiDeliveryConfig:
    return ApiDeliveryConfig(
        name=row.name,
        url=row.url,
        method=row.method,
        headers=dict(row.headers or {}),
        body=row.body,
        response_path=row.response_path,
        timeout_seconds=float(row.timeout_seconds),
        enabled=bool(row.enabled),
        description=row.description,
        local_id=row.id,
    )


class SqlAlchemyApiConfigRepository:
    """外部发卡 API 配置的仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, config: ApiDeliveryConfig) -> int:
        row = ApiDeliveryConfigRow(
            name=config.name,
            url=config.url,
            method=config.method,
            headers=dict(config.headers) or None,
            body=config.body,
            response_path=config.response_path,
            timeout_seconds=config.timeout_seconds,
            enabled=config.enabled,
            description=config.description,
        )
        self._session.add(row)
        await self._session.flush()
        return int(row.id)

    async def get(self, config_id: int) -> ApiDeliveryConfig | None:
        row = await self._session.get(ApiDeliveryConfigRow, config_id)
        return None if row is None else _to_api_config(row)

    async def get_by_name(self, name: str) -> ApiDeliveryConfig | None:
        row = await self._session.scalar(
            select(ApiDeliveryConfigRow).where(ApiDeliveryConfigRow.name == name).limit(1)
        )
        return None if row is None else _to_api_config(row)

    async def list_configs(
        self, *, only_enabled: bool = False, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[ApiDeliveryConfig]:
        statement = select(ApiDeliveryConfigRow).order_by(ApiDeliveryConfigRow.id).limit(limit)
        if only_enabled:
            statement = statement.where(ApiDeliveryConfigRow.enabled.is_(True))
        rows = (await self._session.scalars(statement)).all()
        return [_to_api_config(row) for row in rows]

    async def apply(self, config: ApiDeliveryConfig) -> None:
        if config.local_id is None:
            msg = "更新 API 配置需要 local_id"
            raise ValueError(msg)
        result = await self._session.execute(
            update(ApiDeliveryConfigRow)
            .where(ApiDeliveryConfigRow.id == config.local_id)
            .values(
                name=config.name,
                url=config.url,
                method=config.method,
                headers=dict(config.headers) or None,
                body=config.body,
                response_path=config.response_path,
                timeout_seconds=config.timeout_seconds,
                enabled=config.enabled,
                description=config.description,
            )
        )
        if _rowcount(result) != 1:
            msg = f"API 配置 #{config.local_id} 不存在"
            raise ValueError(msg)

    async def set_enabled(self, config_id: int, *, enabled: bool) -> None:
        result = await self._session.execute(
            update(ApiDeliveryConfigRow)
            .where(ApiDeliveryConfigRow.id == config_id)
            .values(enabled=enabled)
        )
        if _rowcount(result) != 1:
            msg = f"API 配置 #{config_id} 不存在"
            raise ValueError(msg)

    async def delete(self, config_id: int) -> None:
        result = await self._session.execute(
            delete(ApiDeliveryConfigRow).where(ApiDeliveryConfigRow.id == config_id)
        )
        if _rowcount(result) != 1:
            msg = f"API 配置 #{config_id} 不存在"
            raise ValueError(msg)
