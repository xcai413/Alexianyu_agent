"""发货守卫:规则判定与 fail closed 汇总(Issue #5 Phase 6, §12/M17)。

守卫回答"这个订单能不能自动发货"。规则本身是纯函数,数据由应用层查好传进来,所以每条规则
都能离线测试。

两条硬规则:
1. 任何一条规则判 BLOCK,整次发货就 BLOCK——不做"多数通过"这类加权。
2. 规则执行出错时不得放行:调用方把异常交到 evaluate_results,结果按 REQUIRE_CONFIRMATION
   处理(人工确认),宁可多问一句也不静默发货。

规则无法判定的情况(例如订单镜像里没有买家信用数据)单独算 UNVERIFIABLE,同样需要人工确认,
不会因为"查不到"就当成没问题。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

RULE_PERSONAL_BLACKLIST = "personal_blacklist"
RULE_BUYER_CREDIT_ZERO = "buyer_credit_zero"
RULE_BUYER_HAS_ORDER = "buyer_has_order"
RULE_BUYER_HAS_ORDER_GLOBAL = "buyer_has_order_global"
RULE_BUYER_UNCONFIRMED = "buyer_unconfirmed"

RULE_CODES: tuple[str, ...] = (
    RULE_PERSONAL_BLACKLIST,
    RULE_BUYER_CREDIT_ZERO,
    RULE_BUYER_HAS_ORDER,
    RULE_BUYER_HAS_ORDER_GLOBAL,
    RULE_BUYER_UNCONFIRMED,
)

DEFAULT_ACTIVE_STATUSES: tuple[str, ...] = ("paid", "delivered")
DEFAULT_UNCONFIRMED_STATUSES: tuple[str, ...] = ("delivered",)


class DeliveryDecision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_CONFIRMATION = "require_confirmation"
    CARD_ONLY = "card_only"


class GuardVerdict(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRE_CONFIRMATION = "require_confirmation"
    NOT_APPLICABLE = "not_applicable"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class GuardRule:
    """一条守卫规则的配置。"""

    rule_code: str
    enabled: bool = True
    priority: int = 0
    block_reason: str | None = None
    excluded_item_ids: tuple[str, ...] = ()
    config: Mapping[str, object] = field(default_factory=dict)
    account_id: str | None = None
    local_id: int | None = None

    def __post_init__(self) -> None:
        if self.rule_code not in RULE_CODES:
            msg = f"未知守卫规则: {self.rule_code!r},可选 {RULE_CODES}"
            raise ValueError(msg)
        if self.priority < 0:
            msg = "规则优先级不能为负数"
            raise ValueError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "规则 id 必须为正整数"
            raise ValueError(msg)

    @property
    def scope_key(self) -> str:
        return self.account_id or "global"

    def applies_to(self, *, account_id: str | None, item_id: str | None) -> bool:
        if not self.enabled:
            return False
        if self.account_id is not None and self.account_id != account_id:
            return False
        return item_id not in self.excluded_item_ids

    def threshold(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            msg = f"规则 {self.rule_code} 的 {key} 必须是非负整数,当前是 {value!r}"
            raise ValueError(msg)
        return value

    def statuses(self, key: str, default: Sequence[str]) -> tuple[str, ...]:
        value = self.config.get(key, tuple(default))
        if not isinstance(value, (list, tuple)) or not value:
            msg = f"规则 {self.rule_code} 的 {key} 必须是非空状态列表"
            raise ValueError(msg)
        entries: list[str] = []
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                msg = f"规则 {self.rule_code} 的 {key} 只能包含状态字符串"
                raise ValueError(msg)
            entries.append(entry.strip())
        return tuple(entries)

    def reason_for(self, fallback: str) -> str:
        return (self.block_reason or fallback).strip() or fallback


@dataclass(frozen=True)
class GuardContext:
    """One delivery request the guard has to judge."""

    account_id: str
    order_id: str
    item_id: str | None = None
    buyer_id: str | None = None
    buyer_name: str | None = None


@dataclass(frozen=True)
class BlacklistEntry:
    """一条买家黑名单:owner 全局、账号级或商品级。"""

    buyer_id: str
    reason: str = ""
    account_id: str | None = None
    item_id: str | None = None
    local_id: int | None = None

    def __post_init__(self) -> None:
        if not self.buyer_id.strip():
            msg = "黑名单必须记录买家"
            raise ValueError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "黑名单 id 必须为正整数"
            raise ValueError(msg)

    def matches(self, context: GuardContext) -> bool:
        if self.buyer_id != context.buyer_id:
            return False
        if self.account_id is not None and self.account_id != context.account_id:
            return False
        return self.item_id is None or self.item_id == context.item_id


@dataclass(frozen=True)
class GuardFacts:
    """Everything the rules need, already queried by the application layer."""

    buyer_active_orders: int = 0
    buyer_active_orders_global: int = 0
    buyer_unconfirmed_orders: int = 0
    blacklist_hits: tuple[BlacklistEntry, ...] = ()
    buyer_credit_known: bool = False
    buyer_credit_reviews: int | None = None
    buyer_credit_score: int | None = None


@dataclass(frozen=True)
class RuleResult:
    """One rule's verdict, kept for the audit trail."""

    rule_code: str
    verdict: str
    reason: str
    priority: int = 0


@dataclass(frozen=True)
class GuardOutcome:
    decision: str
    reason: str
    results: tuple[RuleResult, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == DeliveryDecision.ALLOW.value

    @property
    def blocked(self) -> bool:
        return self.decision == DeliveryDecision.BLOCK.value

    @property
    def needs_human(self) -> bool:
        return self.decision in {
            DeliveryDecision.REQUIRE_CONFIRMATION.value,
            DeliveryDecision.BLOCK.value,
        }

    def hit_rule_codes(self) -> tuple[str, ...]:
        return tuple(
            result.rule_code
            for result in self.results
            if result.verdict in {GuardVerdict.BLOCK.value, GuardVerdict.REQUIRE_CONFIRMATION.value}
        )


def _evaluate_blacklist(rule: GuardRule, facts: GuardFacts, context: GuardContext) -> RuleResult:
    for entry in facts.blacklist_hits:
        if entry.matches(context):
            reason = rule.reason_for(
                f"买家在黑名单里{(': ' + entry.reason) if entry.reason else ''}"
            )
            return RuleResult(rule.rule_code, GuardVerdict.BLOCK.value, reason, rule.priority)
    return RuleResult(
        rule.rule_code, GuardVerdict.NOT_APPLICABLE.value, "买家不在黑名单里", rule.priority
    )


def _evaluate_buyer_has_order(
    rule: GuardRule, facts: GuardFacts, _context: GuardContext
) -> RuleResult:
    threshold = rule.threshold("threshold", 1)
    if facts.buyer_active_orders >= threshold:
        return RuleResult(
            rule.rule_code,
            GuardVerdict.BLOCK.value,
            rule.reason_for(
                f"买家在本账号已有 {facts.buyer_active_orders} 个未完成订单(阈值 {threshold})"
            ),
            rule.priority,
        )
    return RuleResult(
        rule.rule_code, GuardVerdict.NOT_APPLICABLE.value, "买家没有其他未完成订单", rule.priority
    )


def _evaluate_buyer_has_order_global(
    rule: GuardRule, facts: GuardFacts, _context: GuardContext
) -> RuleResult:
    threshold = rule.threshold("threshold", 1)
    if facts.buyer_active_orders_global >= threshold:
        return RuleResult(
            rule.rule_code,
            GuardVerdict.BLOCK.value,
            rule.reason_for(
                f"买家在本店其他账号已有 {facts.buyer_active_orders_global} 个未完成订单"
                f"(阈值 {threshold})"
            ),
            rule.priority,
        )
    return RuleResult(
        rule.rule_code,
        GuardVerdict.NOT_APPLICABLE.value,
        "买家没有跨账号未完成订单",
        rule.priority,
    )


def _evaluate_buyer_unconfirmed(
    rule: GuardRule, facts: GuardFacts, _context: GuardContext
) -> RuleResult:
    threshold = rule.threshold("threshold", 1)
    if facts.buyer_unconfirmed_orders >= threshold:
        return RuleResult(
            rule.rule_code,
            GuardVerdict.BLOCK.value,
            rule.reason_for(
                f"买家有 {facts.buyer_unconfirmed_orders} 个已发货未确认收货的订单"
                f"(阈值 {threshold})"
            ),
            rule.priority,
        )
    return RuleResult(
        rule.rule_code, GuardVerdict.NOT_APPLICABLE.value, "买家没有未确认收货订单", rule.priority
    )


def _evaluate_buyer_credit_zero(
    rule: GuardRule, facts: GuardFacts, _context: GuardContext
) -> RuleResult:
    if not facts.buyer_credit_known:
        return RuleResult(
            rule.rule_code,
            GuardVerdict.UNVERIFIABLE.value,
            "订单镜像里还没有买家信用与评价数,无法判定,需要人工确认",
            rule.priority,
        )
    minimum = rule.threshold("min_reviews", 1)
    reviews = facts.buyer_credit_reviews or 0
    score = facts.buyer_credit_score
    bad_score = score is not None and score <= 0
    if reviews < minimum or bad_score:
        detail = f"评价数 {reviews}(阈值 {minimum})" + (
            f",信用 {score}" if score is not None else ""
        )
        return RuleResult(
            rule.rule_code,
            GuardVerdict.BLOCK.value,
            rule.reason_for(f"买家信用信息不满足要求: {detail}"),
            rule.priority,
        )
    return RuleResult(
        rule.rule_code, GuardVerdict.NOT_APPLICABLE.value, "买家信用信息满足要求", rule.priority
    )


_RULE_EVALUATORS: dict[str, object] = {
    RULE_PERSONAL_BLACKLIST: _evaluate_blacklist,
    RULE_BUYER_HAS_ORDER: _evaluate_buyer_has_order,
    RULE_BUYER_HAS_ORDER_GLOBAL: _evaluate_buyer_has_order_global,
    RULE_BUYER_UNCONFIRMED: _evaluate_buyer_unconfirmed,
    RULE_BUYER_CREDIT_ZERO: _evaluate_buyer_credit_zero,
}


def evaluate_rule(rule: GuardRule, facts: GuardFacts, context: GuardContext) -> RuleResult:
    """Evaluate one rule against pre-queried facts."""
    evaluator = _RULE_EVALUATORS.get(rule.rule_code)
    if evaluator is None:
        msg = f"未实现的守卫规则: {rule.rule_code}"
        raise ValueError(msg)
    return evaluator(rule, facts, context)  # type: ignore[operator]


def evaluate_results(results: Iterable[RuleResult]) -> GuardOutcome:
    """Aggregate verdicts fail closed: any block blocks, unverifiable asks a human."""
    collected = tuple(results)
    blocks = [result for result in collected if result.verdict == GuardVerdict.BLOCK.value]
    if blocks:
        winner = sorted(blocks, key=lambda result: -result.priority)[0]
        return GuardOutcome(
            decision=DeliveryDecision.BLOCK.value, reason=winner.reason, results=collected
        )
    confirmations = [
        result
        for result in collected
        if result.verdict
        in {GuardVerdict.REQUIRE_CONFIRMATION.value, GuardVerdict.UNVERIFIABLE.value}
    ]
    if confirmations:
        winner = sorted(confirmations, key=lambda result: -result.priority)[0]
        return GuardOutcome(
            decision=DeliveryDecision.REQUIRE_CONFIRMATION.value,
            reason=winner.reason,
            results=collected,
        )
    return GuardOutcome(
        decision=DeliveryDecision.ALLOW.value, reason="所有启用的守卫规则均通过", results=collected
    )


def evaluate_rules(
    rules: Sequence[GuardRule], facts: GuardFacts, context: GuardContext
) -> GuardOutcome:
    """Run every applicable rule, then aggregate."""
    applicable = [
        rule
        for rule in rules
        if rule.applies_to(account_id=context.account_id, item_id=context.item_id)
    ]
    results = [evaluate_rule(rule, facts, context) for rule in applicable]
    return evaluate_results(results)


def failed_rule_result(rule_code: str, error: BaseException) -> RuleResult:
    """A rule that raised must never turn into an allow (M17)."""
    return RuleResult(
        rule_code=rule_code,
        verdict=GuardVerdict.REQUIRE_CONFIRMATION.value,
        reason=f"规则 {rule_code} 执行失败,按 fail closed 处理: {type(error).__name__}",
    )
