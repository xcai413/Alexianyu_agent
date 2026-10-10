"""Dry run, guardrail and audit tests for item lifecycle actions (M14)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.application.item.lifecycle_service import (
    LIFECYCLE_ACTION,
    ItemLifecycleService,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Account, AuditLog, Order, ReplyLog, database as db_mod
from xianyu_agent.domain import (
    accounts as domain_accounts,
    items as domain_items,
    messages as domain_messages,
)
from xianyu_agent.domain.item.filter_policy import ItemFilter
from xianyu_agent.domain.item.lifecycle_policy import LifecyclePolicy
from xianyu_agent.infrastructure.database.models.item import Item
from xianyu_agent.protocol.events import MessageContentType, MessageReceived
from xianyu_agent.protocol.items_client import RemoteItem

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "lifecycle.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _remote(item_id: str) -> RemoteItem:
    return RemoteItem(
        item_id=item_id,
        title=f"商品 {item_id}",
        price="45",
        status="1",
        detail_url=None,
        main_image_url=None,
        category_id="123",
        auction_type="a",
        raw_payload={"id": item_id},
    )


async def _seed_items(days_old: dict[str, int]) -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
    await domain_items.apply_on_sale_snapshot("acc-1", [_remote(item_id) for item_id in days_old])
    async with db_mod.get_async_session() as session:
        for item_id, days in days_old.items():
            row = (await session.execute(select(Item).where(Item.item_id == item_id))).scalar_one()
            row.first_seen_at = NOW - timedelta(days=days)
        await session.commit()


async def _account_pk() -> int:
    async with db_mod.get_async_session() as session:
        return (
            await session.execute(select(Account.id).where(Account.account_id == "acc-1"))
        ).scalar_one()


async def _seed_reply_activity(item_id: str, *, days_ago: int) -> None:
    message_id = await domain_messages.upsert_inbound(
        MessageReceived(
            event_id=f"e-{item_id}",
            account_id="acc-1",
            received_at=NOW - timedelta(days=days_ago),
            chat_id="chat-1",
            message_id=f"m-{item_id}",
            sender_id="buyer-1",
            item_id=item_id,
            content_type=MessageContentType.TEXT,
            content="在吗",
        )
    )
    async with db_mod.get_async_session() as session:
        session.add(
            ReplyLog(
                account_id=await _account_pk(),
                message_id=message_id,
                sent_text="在的",
                success=True,
                sent_at=NOW - timedelta(days=days_ago),
            )
        )
        await session.commit()


async def _seed_order_activity(item_id: str, *, days_ago: int) -> None:
    async with db_mod.get_async_session() as session:
        session.add(
            Order(
                account_id=await _account_pk(),
                order_id=f"O-{item_id}",
                item_id=item_id,
                amount=45.0,
                quantity=1,
                status="paid",
                paid_at=NOW - timedelta(days=days_ago),
            )
        )
        await session.commit()


async def _seed_executed_actions(count: int) -> None:
    async with db_mod.get_async_session() as session:
        for index in range(count):
            session.add(
                AuditLog(
                    actor="system",
                    action=LIFECYCLE_ACTION,
                    target="acc-1",
                    params={"item_id": f"I-{index}", "decision": "DELIST", "reason": "test"},
                    result="executed",
                    created_at=NOW,
                )
            )
        await session.commit()


async def _lifecycle_audits() -> list[AuditLog]:
    async with db_mod.get_async_session() as session:
        return list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == LIFECYCLE_ACTION)
                    .order_by(AuditLog.id.asc())
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_plan_is_a_dry_run_that_changes_nothing(clean_db) -> None:
    await _seed_items({"I-OLD": 30, "I-NEW": 1})
    service = ItemLifecycleService()

    plan = await service.plan(
        "acc-1", policy=LifecyclePolicy(enabled=True, delist_after_days=10), now=NOW
    )

    assert plan.counts == {"KEEP": 1, "DELIST": 1, "DELETE": 0}
    assert [row.item.external_item_id for row in plan.actionable] == ["I-OLD"]
    async with db_mod.get_async_session() as session:
        still_on_sale = (
            await session.execute(select(Item.is_on_sale).where(Item.item_id == "I-OLD"))
        ).scalar_one()
    assert still_on_sale is True
    audits = await _lifecycle_audits()
    assert [row.result for row in audits] == ["dry_run"]
    assert "可执行 1 项" in str(audits[0].params)


@pytest.mark.asyncio
async def test_recent_reply_or_order_activity_protects_an_old_item(clean_db) -> None:
    await _seed_items({"I-REPLY": 30, "I-ORDER": 30, "I-IDLE": 30})
    await _seed_reply_activity("I-REPLY", days_ago=1)
    await _seed_order_activity("I-ORDER", days_ago=2)
    service = ItemLifecycleService()

    plan = await service.plan(
        "acc-1", policy=LifecyclePolicy(enabled=True, delist_after_days=10), now=NOW
    )

    reasons = {row.item.external_item_id: row.decision.reason for row in plan.rows}
    assert reasons["I-REPLY"] == "回复活动保护(7 天)"
    assert reasons["I-ORDER"] == "订单活动保护(7 天)"
    assert reasons["I-IDLE"].startswith("上架 30 天")


@pytest.mark.asyncio
async def test_apply_runs_the_executor_and_writes_executed_audit(clean_db) -> None:
    await _seed_items({"I-OLD": 30})
    calls: list[tuple[str, str, str]] = []

    async def executor(account_id: str, item: object, action: str) -> bool:
        calls.append((account_id, item.external_item_id, action))  # type: ignore[attr-defined]
        return True

    service = ItemLifecycleService(executor=executor)
    plan = await service.plan(
        "acc-1", policy=LifecyclePolicy(enabled=True, delist_after_days=10), now=NOW
    )
    result = await service.apply(plan)

    assert calls == [("acc-1", "I-OLD", "DELIST")]
    assert (result.executed, result.failed) == (1, 0)
    audits = await _lifecycle_audits()
    assert [row.result for row in audits] == ["dry_run", "executed"]
    assert audits[1].params["item_id"] == "I-OLD"


@pytest.mark.asyncio
async def test_delete_requires_explicit_opt_in(clean_db) -> None:
    await _seed_items({"I-OLD": 90})
    executed: list[str] = []

    async def executor(_account_id: str, item: object, action: str) -> bool:
        executed.append(action)
        return True

    service = ItemLifecycleService(executor=executor)
    policy = LifecyclePolicy(enabled=True, delist_after_days=10, delete_after_days=60)
    plan = await service.plan("acc-1", policy=policy, now=NOW)
    assert plan.actionable[0].decision.action == "DELETE"

    blocked = await service.apply(plan, allow_delete=False)
    assert executed == []
    assert blocked.executed == 0
    assert blocked.results[0].error == "未开启删除动作"
    assert [row.result for row in await _lifecycle_audits()][-1] == "blocked"

    allowed = await service.apply(plan, allow_delete=True)
    assert executed == ["DELETE"]
    assert allowed.executed == 1


@pytest.mark.asyncio
async def test_apply_without_executor_refuses_and_executor_failure_is_audited(clean_db) -> None:
    await _seed_items({"I-OLD": 30})
    policy = LifecyclePolicy(enabled=True, delist_after_days=10)
    misconfigured = ItemLifecycleService()
    plan = await misconfigured.plan("acc-1", policy=policy, now=NOW)

    with pytest.raises(RuntimeError, match="未配置商品动作执行器"):
        await misconfigured.apply(plan)

    async def failing(_account_id: str, _item: object, _action: str) -> bool:
        raise TimeoutError("平台未响应")

    failing_service = ItemLifecycleService(executor=failing)
    result = await failing_service.apply(plan)

    assert result.executed == 0
    assert result.results[0].error == "TimeoutError"
    assert [row.result for row in await _lifecycle_audits()][-1] == "failed"


@pytest.mark.asyncio
async def test_filter_narrows_the_plan_and_daily_limit_stops_actions(clean_db) -> None:
    await _seed_items({"I-OLD": 30, "I-OTHER": 30})
    await _seed_executed_actions(2)
    service = ItemLifecycleService()
    policy = LifecyclePolicy(enabled=True, delist_after_days=10, daily_limit=2)

    limited = await service.plan("acc-1", policy=policy, now=NOW)

    assert limited.counts["KEEP"] == 2
    assert all("上限" in row.decision.reason for row in limited.rows)

    scoped = await service.plan(
        "acc-1", policy=policy, item_filter=ItemFilter(include_keywords=("I-OLD",)), now=NOW
    )
    assert [row.item.external_item_id for row in scoped.rows] == ["I-OLD"]


@pytest.mark.asyncio
async def test_plan_reports_truncation_instead_of_silently_scanning_a_subset(
    clean_db,
) -> None:
    await _seed_items({"I-1": 30, "I-2": 30, "I-3": 30})
    service = ItemLifecycleService()
    policy = LifecyclePolicy(enabled=True, delist_after_days=10)

    full = await service.plan("acc-1", policy=policy, limit=10, now=NOW)
    limited = await service.plan("acc-1", policy=policy, limit=2, now=NOW)

    assert full.truncated is False
    assert full.scanned == 3
    # 只扫到 2 件时必须标记截断,否则运营会以为这 2 件就是全部。
    assert limited.truncated is True
    assert limited.scanned == 2
    assert len(limited.rows) == 2
    audits = await _lifecycle_audits()
    assert "还有更多未纳入" in str(audits[-1].params)
