"""发货守卫的应用层:取数、跑规则、fail closed 汇总(Issue #5 Phase 6, §12/M17)。

规则本身是纯函数(domain/delivery/guard.py),这一层负责把判定需要的事实在同一个会话里查出来:
黑名单、买家在本账号/跨账号的未完成订单数、未确认收货订单数。买家信用数据当前镜像里没有,
所以那条规则会明确返回"无法判定"而不是偷偷放行。

任何规则抛异常都在这里被兜住,并按 fail closed 转成"需要人工确认"——守卫出错时绝不放行。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from xianyu_agent.db import get_async_session
from xianyu_agent.domain.delivery import (
    DEFAULT_ACTIVE_STATUSES,
    DEFAULT_UNCONFIRMED_STATUSES,
    RULE_BUYER_CREDIT_ZERO,
    RULE_BUYER_HAS_ORDER,
    RULE_BUYER_HAS_ORDER_GLOBAL,
    RULE_BUYER_UNCONFIRMED,
    RULE_CODES,
    RULE_PERSONAL_BLACKLIST,
    BlacklistEntry,
    GuardContext,
    GuardFacts,
    GuardOutcome,
    GuardRule,
    evaluate_results,
    evaluate_rule,
    failed_rule_result,
)
from xianyu_agent.infrastructure.database.repositories import SqlAlchemyDeliveryGuardRepository


class DeliveryGuardService:
    """Evaluate delivery guard rules and maintain their configuration."""

    async def evaluate(self, context: GuardContext) -> GuardOutcome:
        """Judge one delivery request; never allows when a rule errors out."""
        async with get_async_session() as session:
            repo = SqlAlchemyDeliveryGuardRepository(session)
            rules = [
                rule
                for rule in await repo.list_rules(account_id=context.account_id, only_enabled=True)
                if rule.applies_to(account_id=context.account_id, item_id=context.item_id)
            ]
            facts = await self._facts(repo, rules, context)
        return self._run_rules(rules, facts, context)

    @staticmethod
    async def _facts(
        repo: SqlAlchemyDeliveryGuardRepository,
        rules: Sequence[GuardRule],
        context: GuardContext,
    ) -> GuardFacts:
        facts = GuardFacts()
        if not context.buyer_id:
            return facts
        facts = replace(
            facts,
            blacklist_hits=tuple(await repo.list_blacklist(buyer_id=context.buyer_id)),
        )
        for rule in rules:
            if rule.rule_code == RULE_BUYER_HAS_ORDER:
                facts = replace(
                    facts,
                    buyer_active_orders=await repo.count_buyer_active_orders(
                        account_id=context.account_id,
                        buyer_id=context.buyer_id,
                        statuses=rule.statuses("statuses", DEFAULT_ACTIVE_STATUSES),
                    ),
                )
            elif rule.rule_code == RULE_BUYER_HAS_ORDER_GLOBAL:
                facts = replace(
                    facts,
                    buyer_active_orders_global=await repo.count_buyer_active_orders_global(
                        buyer_id=context.buyer_id,
                        statuses=rule.statuses("statuses", DEFAULT_ACTIVE_STATUSES),
                    ),
                )
            elif rule.rule_code == RULE_BUYER_UNCONFIRMED:
                facts = replace(
                    facts,
                    buyer_unconfirmed_orders=await repo.count_buyer_unconfirmed_orders(
                        buyer_id=context.buyer_id,
                        statuses=rule.statuses("statuses", DEFAULT_UNCONFIRMED_STATUSES),
                    ),
                )
            elif rule.rule_code == RULE_BUYER_CREDIT_ZERO:
                # 订单镜像里没有买家信用与评价数:保持 buyer_credit_known=False,
                # 规则会返回"无法判定",由人工确认。
                facts = replace(facts, buyer_credit_known=False)
        return facts

    @staticmethod
    def _run_rules(
        rules: Sequence[GuardRule], facts: GuardFacts, context: GuardContext
    ) -> GuardOutcome:
        results = []
        for rule in rules:
            try:
                results.append(evaluate_rule(rule, facts, context))
            except Exception as exc:
                results.append(failed_rule_result(rule.rule_code, exc))
        return evaluate_results(results)

    # ---- 规则配置 --------------------------------------------------------

    async def list_rules(
        self, *, account_id: str | None = None, only_enabled: bool = False
    ) -> list[GuardRule]:
        async with get_async_session() as session:
            return await SqlAlchemyDeliveryGuardRepository(session).list_rules(
                account_id=account_id, only_enabled=only_enabled
            )

    async def upsert_rule(
        self,
        *,
        rule_code: str,
        account_id: str | None = None,
        enabled: bool = True,
        priority: int = 0,
        block_reason: str | None = None,
        excluded_item_ids: Sequence[str] = (),
        config: dict[str, object] | None = None,
    ) -> GuardRule:
        rule = GuardRule(
            rule_code=rule_code,
            enabled=enabled,
            priority=priority,
            block_reason=block_reason,
            excluded_item_ids=tuple(excluded_item_ids),
            config=dict(config or {}),
            account_id=account_id,
        )
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryGuardRepository(session)
            rule_id = await repo.upsert_rule(rule)
            stored = await repo.get_rule(rule_id)
        if stored is None:  # pragma: no cover - 刚写入就消失只可能是并发删除
            msg = f"守卫规则 #{rule_id} 写入后读取失败"
            raise RuntimeError(msg)
        return stored

    async def set_rule_enabled(self, rule_id: int, *, enabled: bool) -> GuardRule:
        async with get_async_session() as session, session.begin():
            repo = SqlAlchemyDeliveryGuardRepository(session)
            await repo.set_rule_enabled(rule_id, enabled=enabled)
            rule = await repo.get_rule(rule_id)
        if rule is None:  # pragma: no cover - 与存在性检查重复
            msg = f"守卫规则 #{rule_id} 不存在"
            raise ValueError(msg)
        return rule

    async def delete_rule(self, rule_id: int) -> None:
        async with get_async_session() as session, session.begin():
            await SqlAlchemyDeliveryGuardRepository(session).delete_rule(rule_id)

    # ---- 黑名单 ----------------------------------------------------------

    async def add_blacklist(
        self,
        *,
        buyer_id: str,
        reason: str = "",
        account_id: str | None = None,
        item_id: str | None = None,
    ) -> BlacklistEntry:
        entry = BlacklistEntry(
            buyer_id=buyer_id, reason=reason, account_id=account_id, item_id=item_id
        )
        async with get_async_session() as session, session.begin():
            entry_id = await SqlAlchemyDeliveryGuardRepository(session).add_blacklist(entry)
        return replace(entry, local_id=entry_id)

    async def list_blacklist(
        self, *, buyer_id: str | None = None, account_id: str | None = None
    ) -> list[BlacklistEntry]:
        async with get_async_session() as session:
            return await SqlAlchemyDeliveryGuardRepository(session).list_blacklist(
                buyer_id=buyer_id, account_id=account_id
            )

    async def delete_blacklist(self, entry_id: int) -> None:
        async with get_async_session() as session, session.begin():
            await SqlAlchemyDeliveryGuardRepository(session).delete_blacklist(entry_id)

    @staticmethod
    def known_rules() -> tuple[str, ...]:
        return RULE_CODES

    @staticmethod
    def blacklist_rule_code() -> str:
        return RULE_PERSONAL_BLACKLIST
