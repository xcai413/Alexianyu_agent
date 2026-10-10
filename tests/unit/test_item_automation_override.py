"""商品级自动化:单品人工覆盖先于策略,并且能被 Dry Run 与执行同时尊重。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.application.item.lifecycle_service import (
    ItemLifecycleService,
    lifecycle_policy,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.domain.item.lifecycle_policy import OVERRIDE_EXCLUDE, OVERRIDE_KEEP
from xianyu_agent.infrastructure.database.models import Account as AccountRow, Item as ItemRow
from xianyu_agent.protocol.items_client import RemoteItem


@pytest.fixture
async def item_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "item-automation.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_items(*item_ids: str, age_days: int) -> None:
    """一次快照写入全部商品:分两次写会把前一批标记成非在售。"""
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id=item_id,
                title=f"商品 {item_id}",
                price="9.9",
                status="1",
                detail_url=None,
                main_image_url=None,
                category_id=None,
                auction_type=None,
                raw_payload=None,
            )
            for item_id in item_ids
        ],
    )
    async with db_mod.async_session_factory() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == "acc-1").limit(1)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(ItemRow).where(
                    ItemRow.account_id == account.id, ItemRow.item_id.in_(item_ids)
                )
            )
        ).scalars()
        for row in rows:
            row.first_seen_at = datetime.now(UTC) - timedelta(days=age_days)


async def test_override_requires_a_reason_for_keep(item_db: None) -> None:
    await _seed_items("I-1", age_days=1)

    with pytest.raises(ValueError, match="必须写明原因"):
        await domain_items.set_automation_override(
            "acc-1", "I-1", mode=OVERRIDE_KEEP, note=""
        )
    with pytest.raises(ValueError, match="未知的人工覆盖模式"):
        await domain_items.set_automation_override("acc-1", "I-1", mode="pause")


async def test_missing_item_is_reported_instead_of_silently_ignored(item_db: None) -> None:
    assert (
        await domain_items.set_automation_override("acc-1", "NOPE", mode=OVERRIDE_KEEP, note="x")
        is False
    )


async def test_excluded_item_disappears_from_the_lifecycle_plan(item_db: None) -> None:
    await _seed_items("I-1", "I-2", age_days=60)
    await domain_items.set_automation_override(
        "acc-1", "I-2", mode=OVERRIDE_EXCLUDE, note="活动商品,暂不处理"
    )
    service = ItemLifecycleService()

    plan = await service.plan(
        "acc-1", policy=lifecycle_policy(delist_after_days=30, enabled=True)
    )

    assert [row.item.external_item_id for row in plan.rows] == ["I-1"]


async def test_kept_item_stays_keep_even_when_it_is_old_enough_to_delist(
    item_db: None,
) -> None:
    await _seed_items("I-1", age_days=60)
    await domain_items.set_automation_override(
        "acc-1", "I-1", mode=OVERRIDE_KEEP, note="主推款"
    )
    service = ItemLifecycleService()

    plan = await service.plan(
        "acc-1", policy=lifecycle_policy(delist_after_days=30, enabled=True)
    )

    assert [row.decision.action for row in plan.rows] == ["KEEP"]
    assert "人工锁定" in plan.rows[0].decision.reason
    assert plan.actionable == ()


async def test_clearing_the_override_returns_the_item_to_policy_judgement(
    item_db: None,
) -> None:
    await _seed_items("I-1", age_days=60)
    await domain_items.set_automation_override(
        "acc-1", "I-1", mode=OVERRIDE_EXCLUDE, note="临时"
    )
    await domain_items.set_automation_override("acc-1", "I-1", mode=None)
    service = ItemLifecycleService()

    plan = await service.plan(
        "acc-1", policy=lifecycle_policy(delist_after_days=30, enabled=True)
    )

    assert [row.decision.action for row in plan.rows] == ["DELIST"]
    assert await domain_items.list_automation_overrides("acc-1") == []


async def test_override_list_reports_who_and_why(item_db: None) -> None:
    await _seed_items("I-1", age_days=1)
    await domain_items.set_automation_override(
        "acc-1", "I-1", mode=OVERRIDE_KEEP, note="周年活动", actor="ops-1"
    )

    rows = await domain_items.list_automation_overrides("acc-1")

    assert len(rows) == 1
    assert rows[0].automation_override == OVERRIDE_KEEP
    assert rows[0].automation_note == "周年活动"
    assert rows[0].automation_overridden_by == "ops-1"
    assert rows[0].automation_overridden_at is not None
