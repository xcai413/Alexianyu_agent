"""Unit tests for item filter and lifecycle policies (Issue #4 M14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from xianyu_agent.domain.item.entity import Item
from xianyu_agent.domain.item.filter_policy import ItemFilter, select_items
from xianyu_agent.domain.item.lifecycle_policy import (
    LifecycleContext,
    LifecyclePolicy,
    decide,
)

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


def _item(**overrides: object) -> Item:
    values: dict[str, object] = {
        "external_item_id": "I-1",
        "account_id": "acc-1",
        "title": "帆布包 全新",
        "price": Decimal("45"),
        "category_id": "123",
        "is_on_sale": True,
    }
    values.update(overrides)
    return Item(**values)  # type: ignore[arg-type]


def _context(**overrides: object) -> LifecycleContext:
    values: dict[str, object] = {"now": NOW, "listed_at": NOW - timedelta(days=30)}
    values.update(overrides)
    return LifecycleContext(**values)  # type: ignore[arg-type]


def test_filter_matches_keywords_price_and_category() -> None:
    policy = ItemFilter(
        include_keywords=("帆布",),
        exclude_keywords=("瑕疵",),
        min_price=Decimal("40"),
        max_price=Decimal("50"),
        category_ids=("123",),
    )

    assert policy.matches(_item()) is True
    assert policy.reasons(_item(title="瑕疵帆布包")) == ("命中排除关键词: 瑕疵",)
    assert policy.reasons(_item(title="水杯")) == ("未命中任何包含关键词",)
    assert policy.reasons(_item(price=Decimal("39"))) == ("价格低于下限(40)",)
    assert policy.reasons(_item(price=Decimal("51"))) == ("价格高于上限(50)",)
    assert policy.reasons(_item(category_id="999")) == ("类目不在范围内",)


def test_filter_excludes_unknown_price_and_off_sale_items() -> None:
    bounded = ItemFilter(min_price=Decimal("10"))
    scope = ItemFilter(on_sale_only=True)

    assert bounded.reasons(_item(price=None)) == ("价格未知",)
    assert ItemFilter().matches(_item(price=None)) is True
    assert scope.reasons(_item(is_on_sale=False)) == ("已非在售",)
    assert ItemFilter(on_sale_only=False).matches(_item(is_on_sale=False)) is True


def test_filter_validation_and_selection() -> None:
    with pytest.raises(ValueError, match="min_price 不能高于"):
        ItemFilter(min_price=Decimal("10"), max_price=Decimal("5"))
    with pytest.raises(ValueError, match="关键词不能为空"):
        ItemFilter(include_keywords=("  ",))

    items = (_item(), _item(external_item_id="I-2", title="水杯"))
    assert [
        item.external_item_id
        for item in select_items(items, ItemFilter(include_keywords=("帆布",)))
    ] == ["I-1"]


def test_lifecycle_is_disabled_by_default() -> None:
    decision = decide(LifecyclePolicy(), _context())

    assert decision.action == "KEEP"
    assert decision.reason == "策略未启用"
    assert decision.actionable is False


def test_activity_protections_win_over_age_thresholds() -> None:
    policy = LifecyclePolicy(enabled=True, delist_after_days=10)

    reply = decide(policy, _context(last_reply_at=NOW - timedelta(days=1)))
    order = decide(policy, _context(last_order_at=NOW - timedelta(days=2)))
    stale_reply = decide(policy, _context(last_reply_at=NOW - timedelta(days=30)))

    assert reply.reason == "回复活动保护(7 天)"
    assert order.reason == "订单活动保护(7 天)"
    assert stale_reply.action == "DELIST"


def test_missing_listing_time_stays_keep() -> None:
    policy = LifecyclePolicy(enabled=True, delist_after_days=1)

    decision = decide(policy, _context(listed_at=None))

    assert decision.action == "KEEP"
    assert decision.reason == "缺少上架时间"


def test_age_thresholds_and_delete_opt_in() -> None:
    delist_only = LifecyclePolicy(enabled=True, delist_after_days=10)
    with_delete = LifecyclePolicy(enabled=True, delist_after_days=10, delete_after_days=60)

    assert decide(delist_only, _context()).action == "DELIST"
    assert decide(with_delete, _context()).action == "DELIST"
    old = decide(with_delete, _context(listed_at=NOW - timedelta(days=90)))
    assert old.action == "DELETE"
    assert "删除阈值" in old.reason
    young = decide(delist_only, _context(listed_at=NOW - timedelta(days=2)))
    assert young.action == "KEEP"


def test_daily_limit_blocks_further_actions() -> None:
    policy = LifecyclePolicy(enabled=True, delist_after_days=10, daily_limit=2)

    blocked = decide(policy, _context(daily_action_count=2))
    allowed = decide(policy, _context(daily_action_count=1, last_reply_at=None))

    assert blocked.action == "KEEP"
    assert "上限" in blocked.reason
    assert allowed.action == "DELIST"
    assert decide(policy, _context(daily_action_count=5)).action == "KEEP"


def test_lifecycle_policy_validation() -> None:
    with pytest.raises(ValueError, match="不能为负数"):
        LifecyclePolicy(delist_after_days=-1)
    with pytest.raises(ValueError, match="daily_limit"):
        LifecyclePolicy(daily_limit=0)
    with pytest.raises(ValueError, match="不能早于"):
        LifecyclePolicy(delist_after_days=30, delete_after_days=10)
