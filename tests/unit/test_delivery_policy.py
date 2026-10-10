"""发货策略的层级、校验与解析优先级(Issue #5 Phase 6, §9)。"""

from __future__ import annotations

import pytest

from xianyu_agent.domain.delivery import (
    LEVEL_ACCOUNT,
    LEVEL_GLOBAL,
    LEVEL_ITEM,
    LEVEL_SKU,
    DeliveryPolicy,
    PolicyError,
    resolve_policy,
    validate_policies,
)


def _policy(**kwargs: object) -> DeliveryPolicy:
    payload: dict[str, object] = {"name": "策略", "inventory_pool_id": 1}
    payload.update(kwargs)
    return DeliveryPolicy(**payload)  # type: ignore[arg-type]


def test_level_is_derived_from_the_scope_fields() -> None:
    assert _policy(local_id=1).level == LEVEL_GLOBAL
    assert _policy(local_id=2, account_id="acc-1").level == LEVEL_ACCOUNT
    assert _policy(local_id=3, item_id="I-1").level == LEVEL_ITEM
    assert _policy(local_id=4, item_id="I-1", sku_id="k1").level == LEVEL_SKU


def test_policy_validation_rejects_broken_configuration() -> None:
    with pytest.raises(PolicyError, match="名称"):
        _policy(name="  ")
    with pytest.raises(PolicyError, match="库存池"):
        _policy(inventory_pool_id=0)
    with pytest.raises(PolicyError, match="延迟发货秒数"):
        _policy(delay_seconds=3601)
    with pytest.raises(PolicyError, match="未知发货模式"):
        _policy(delivery_mode="telepathy")
    with pytest.raises(PolicyError, match="必须同时指定商品"):
        _policy(sku_id="k1")
    with pytest.raises(PolicyError, match="优先级"):
        _policy(priority=-1)
    with pytest.raises(PolicyError, match="模板不能是空白"):
        _policy(template="   ")


def test_template_is_validated_at_construction() -> None:
    policy = _policy(template="买家{buyer_name} 卡密{card}")

    assert policy.template_variables == ("buyer_name", "card")

    with pytest.raises(ValueError, match="不在白名单内"):
        _policy(template="{eval(card)}")


def test_scope_key_is_stable_and_explicit() -> None:
    assert _policy().scope_key() == "global"
    assert _policy(account_id="acc-1").scope_key() == "account=acc-1"
    assert _policy(item_id="I-1", sku_id="k1").scope_key() == "item=I-1|sku=k1"


def test_specific_level_wins_over_general() -> None:
    policies = [
        _policy(name="全局", inventory_pool_id=1, local_id=1),
        _policy(name="账号", inventory_pool_id=2, account_id="acc-1", local_id=2),
        _policy(name="商品", inventory_pool_id=3, item_id="I-1", local_id=3),
        _policy(name="规格", inventory_pool_id=4, item_id="I-1", sku_id="k1", local_id=4),
    ]

    resolution = resolve_policy(policies, account_id="acc-1", item_id="I-1", sku_id="k1")

    assert resolution.matched is True
    assert resolution.level == LEVEL_SKU
    assert resolution.policy is not None
    assert resolution.policy.inventory_pool_id == 4
    assert "sku" in resolution.reason


def test_resolution_falls_back_level_by_level() -> None:
    policies = [
        _policy(name="全局", inventory_pool_id=1, local_id=1),
        _policy(name="账号", inventory_pool_id=2, account_id="acc-1", local_id=2),
    ]

    item_only = resolve_policy(policies, account_id="acc-1", item_id="I-1", sku_id="k1")
    unknown_account = resolve_policy(policies, account_id="acc-2", item_id="I-1")

    assert item_only.level == LEVEL_ACCOUNT
    assert unknown_account.level == LEVEL_GLOBAL


def test_item_level_policy_with_account_scope_must_match_account() -> None:
    policies = [_policy(name="商品", inventory_pool_id=3, account_id="acc-1", item_id="I-1")]

    assert resolve_policy(policies, account_id="acc-1", item_id="I-1").matched is True
    assert resolve_policy(policies, account_id="acc-2", item_id="I-1").matched is False


def test_tie_break_uses_priority_then_id() -> None:
    policies = [
        _policy(name="低", inventory_pool_id=1, item_id="I-1", priority=1, local_id=5),
        _policy(name="高", inventory_pool_id=2, item_id="I-1", priority=9, local_id=6),
    ]

    resolution = resolve_policy(policies, item_id="I-1")

    assert resolution.policy is not None
    assert resolution.policy.name == "高"

    same_priority = [
        _policy(name="先", inventory_pool_id=1, item_id="I-1", priority=5, local_id=2),
        _policy(name="后", inventory_pool_id=2, item_id="I-1", priority=5, local_id=3),
    ]
    assert resolve_policy(same_priority, item_id="I-1").policy.name == "先"  # type: ignore[union-attr]


def test_disabled_policies_are_ignored() -> None:
    policies = [
        _policy(name="停用", item_id="I-1", local_id=1, enabled=False),
        _policy(name="全局", inventory_pool_id=2, local_id=2),
    ]

    resolution = resolve_policy(policies, item_id="I-1")

    assert resolution.level == LEVEL_GLOBAL
    assert resolution.policy is not None
    assert resolution.policy.name == "全局"


def test_no_match_explains_itself() -> None:
    resolution = resolve_policy([], account_id="acc-1")

    assert resolution.matched is False
    assert resolution.level is None
    assert "没有任何启用的策略" in resolution.reason


def test_validate_policies_flags_duplicate_scopes() -> None:
    policies = [
        _policy(name="A", account_id="acc-1", local_id=1),
        _policy(name="B", account_id="acc-1", local_id=2),
        _policy(name="C", account_id="acc-1", local_id=3, enabled=False),
    ]

    problems = validate_policies(policies)

    assert len(problems) == 1
    assert "重复配置" in problems[0]
