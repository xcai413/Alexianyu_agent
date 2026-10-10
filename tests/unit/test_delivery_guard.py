"""发货守卫规则判定(Issue #5 Phase 6, §12/M17)。"""

from __future__ import annotations

import pytest

from xianyu_agent.domain.delivery import (
    RULE_BUYER_CREDIT_ZERO,
    RULE_BUYER_HAS_ORDER,
    RULE_BUYER_HAS_ORDER_GLOBAL,
    RULE_BUYER_UNCONFIRMED,
    RULE_PERSONAL_BLACKLIST,
    BlacklistEntry,
    DeliveryDecision,
    GuardContext,
    GuardFacts,
    GuardRule,
    GuardVerdict,
    RuleResult,
    evaluate_results,
    evaluate_rules,
    failed_rule_result,
)

CONTEXT = GuardContext(account_id="acc-1", order_id="O-1", item_id="I-1", buyer_id="B-1")


def _rule(code: str, **kwargs: object) -> GuardRule:
    payload: dict[str, object] = {"rule_code": code}
    payload.update(kwargs)
    return GuardRule(**payload)  # type: ignore[arg-type]


def test_rule_validation_and_scope() -> None:
    with pytest.raises(ValueError, match="未知守卫规则"):
        _rule("not_a_rule")
    with pytest.raises(ValueError, match="优先级"):
        _rule(RULE_BUYER_HAS_ORDER, priority=-1)

    assert (
        _rule(RULE_BUYER_HAS_ORDER, enabled=False).applies_to(account_id="acc-1", item_id="I-1")
        is False
    )
    assert (
        _rule(RULE_BUYER_HAS_ORDER, account_id="acc-2").applies_to(
            account_id="acc-1", item_id="I-1"
        )
        is False
    )
    assert (
        _rule(RULE_BUYER_HAS_ORDER, excluded_item_ids=("I-1",)).applies_to(
            account_id="acc-1", item_id="I-1"
        )
        is False
    )
    assert _rule(RULE_BUYER_HAS_ORDER).applies_to(account_id="acc-1", item_id="I-2") is True


def test_rule_config_parsing_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="threshold"):
        _rule(RULE_BUYER_HAS_ORDER, config={"threshold": "two"}).threshold("threshold", 1)
    with pytest.raises(ValueError, match="threshold"):
        _rule(RULE_BUYER_HAS_ORDER, config={"threshold": True}).threshold("threshold", 1)
    with pytest.raises(ValueError, match="statuses"):
        _rule(RULE_BUYER_HAS_ORDER, config={"statuses": []}).statuses("statuses", ("paid",))
    with pytest.raises(ValueError, match="状态字符串"):
        _rule(RULE_BUYER_HAS_ORDER, config={"statuses": [1]}).statuses("statuses", ("paid",))


def test_blacklist_matching_levels() -> None:
    global_entry = BlacklistEntry(buyer_id="B-1", reason="恶意退款")
    account_entry = BlacklistEntry(buyer_id="B-1", account_id="acc-1")
    other_account = BlacklistEntry(buyer_id="B-1", account_id="acc-2")
    item_entry = BlacklistEntry(buyer_id="B-1", item_id="I-2")
    other_buyer = BlacklistEntry(buyer_id="B-2")

    assert global_entry.matches(CONTEXT) is True
    assert account_entry.matches(CONTEXT) is True
    assert other_account.matches(CONTEXT) is False
    assert item_entry.matches(CONTEXT) is False
    assert other_buyer.matches(CONTEXT) is False
    with pytest.raises(ValueError, match="买家"):
        BlacklistEntry(buyer_id="  ")


def test_blacklist_rule_blocks_with_reason() -> None:
    rule = _rule(RULE_PERSONAL_BLACKLIST)
    facts = GuardFacts(blacklist_hits=(BlacklistEntry(buyer_id="B-1", reason="恶意退款"),))

    outcome = evaluate_rules([rule], facts, CONTEXT)

    assert outcome.decision == DeliveryDecision.BLOCK.value
    assert outcome.blocked is True
    assert "恶意退款" in outcome.reason
    assert outcome.hit_rule_codes() == (RULE_PERSONAL_BLACKLIST,)


def test_buyer_order_rules_respect_thresholds() -> None:
    rule = _rule(RULE_BUYER_HAS_ORDER, config={"threshold": 2})

    under = evaluate_rules([rule], GuardFacts(buyer_active_orders=1), CONTEXT)
    over = evaluate_rules([rule], GuardFacts(buyer_active_orders=2), CONTEXT)

    assert under.decision == DeliveryDecision.ALLOW.value
    assert over.decision == DeliveryDecision.BLOCK.value
    assert "阈值 2" in over.reason


def test_global_and_unconfirmed_rules() -> None:
    global_rule = _rule(RULE_BUYER_HAS_ORDER_GLOBAL, config={"threshold": 1})
    unconfirmed = _rule(RULE_BUYER_UNCONFIRMED, config={"threshold": 3})

    assert (
        evaluate_rules([global_rule], GuardFacts(buyer_active_orders_global=1), CONTEXT).decision
        == DeliveryDecision.BLOCK.value
    )
    assert (
        evaluate_rules([unconfirmed], GuardFacts(buyer_unconfirmed_orders=2), CONTEXT).decision
        == DeliveryDecision.ALLOW.value
    )
    assert (
        evaluate_rules([unconfirmed], GuardFacts(buyer_unconfirmed_orders=3), CONTEXT).decision
        == DeliveryDecision.BLOCK.value
    )


def test_credit_rule_is_unverifiable_without_data() -> None:
    rule = _rule(RULE_BUYER_CREDIT_ZERO)

    outcome = evaluate_rules([rule], GuardFacts(), CONTEXT)

    assert outcome.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value
    assert outcome.needs_human is True
    assert "无法判定" in outcome.reason


def test_credit_rule_blocks_when_data_says_so() -> None:
    rule = _rule(RULE_BUYER_CREDIT_ZERO, config={"min_reviews": 5})
    facts = GuardFacts(buyer_credit_known=True, buyer_credit_reviews=2, buyer_credit_score=0)

    outcome = evaluate_rules([rule], facts, CONTEXT)

    assert outcome.decision == DeliveryDecision.BLOCK.value
    assert "评价数 2" in outcome.reason


def test_block_beats_confirmation_and_reason_uses_priority() -> None:
    results = [
        RuleResult(rule_code="a", verdict=GuardVerdict.UNVERIFIABLE.value, reason="无法判定"),
        RuleResult(rule_code="b", verdict=GuardVerdict.BLOCK.value, reason="低优先", priority=1),
        RuleResult(rule_code="c", verdict=GuardVerdict.BLOCK.value, reason="高优先", priority=9),
    ]

    outcome = evaluate_results(results)

    assert outcome.decision == DeliveryDecision.BLOCK.value
    assert outcome.reason == "高优先"


def test_all_rules_passing_means_allow() -> None:
    outcome = evaluate_results(
        [RuleResult(rule_code="a", verdict=GuardVerdict.NOT_APPLICABLE.value, reason="不适用")]
    )

    assert outcome.allowed is True
    assert outcome.hit_rule_codes() == ()


def test_rule_failure_fails_closed() -> None:
    result = failed_rule_result(RULE_BUYER_HAS_ORDER, ValueError("boom"))

    outcome = evaluate_results([result])

    assert outcome.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value
    assert "fail closed" in outcome.reason
    assert "ValueError" in outcome.reason


def test_disabled_rules_are_not_evaluated() -> None:
    rule = _rule(RULE_PERSONAL_BLACKLIST, enabled=False)
    facts = GuardFacts(blacklist_hits=(BlacklistEntry(buyer_id="B-1"),))

    assert evaluate_rules([rule], facts, CONTEXT).decision == DeliveryDecision.ALLOW.value
