"""发货守卫应用层与 CLI 接线(Issue #5 Phase 6, 6F)。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from typer.testing import CliRunner

from xianyu_agent.application.delivery import DeliveryGuardService
from xianyu_agent.cli.main import app
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.delivery import (
    RULE_BUYER_CREDIT_ZERO,
    RULE_BUYER_HAS_ORDER,
    RULE_BUYER_HAS_ORDER_GLOBAL,
    RULE_BUYER_UNCONFIRMED,
    RULE_PERSONAL_BLACKLIST,
    DeliveryDecision,
    GuardContext,
    GuardRule,
)
from xianyu_agent.infrastructure.database.models import Account as AccountRow, Order as OrderRow

runner = CliRunner()


@pytest.fixture
def wired_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "guard.db"))
    monkeypatch.setenv("XIANYU_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    db_mod.reset_engine()
    asyncio.run(db_mod.init_db())
    yield tmp_path
    asyncio.run(db_mod.async_engine.dispose())
    reset_settings_cache()


async def _seed_order(account_id: str, order_id: str, *, buyer_id: str, status: str) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == account_id).limit(1)
            )
        ).scalar_one_or_none()
        if account is None:
            account = await domain_accounts.create_account(account_id, enabled=True)
            account = (
                await session.execute(
                    select(AccountRow).where(AccountRow.account_id == account_id).limit(1)
                )
            ).scalar_one()
        session.add(
            OrderRow(
                account_id=account.id,
                order_id=order_id,
                buyer_id=buyer_id,
                status=status,
                amount=10.0,
            )
        )


def _context(**kwargs: object) -> GuardContext:
    payload: dict[str, object] = {
        "account_id": "acc-1",
        "order_id": "O-1",
        "buyer_id": "B-1",
    }
    payload.update(kwargs)
    return GuardContext(**payload)  # type: ignore[arg-type]


async def test_no_rules_allows_delivery(wired_db: Path) -> None:
    outcome = await DeliveryGuardService().evaluate(_context())

    assert outcome.decision == DeliveryDecision.ALLOW.value
    assert outcome.allowed is True


async def test_blacklist_blocks_delivery(wired_db: Path) -> None:
    service = DeliveryGuardService()
    await service.upsert_rule(rule_code=RULE_PERSONAL_BLACKLIST)
    await service.add_blacklist(buyer_id="B-1", reason="拒收多次")

    outcome = await service.evaluate(_context())

    assert outcome.decision == DeliveryDecision.BLOCK.value
    assert "拒收多次" in outcome.reason


async def test_buyer_has_order_uses_real_orders(wired_db: Path) -> None:
    await _seed_order("acc-1", "O-0", buyer_id="B-1", status="paid")
    service = DeliveryGuardService()
    await service.upsert_rule(rule_code=RULE_BUYER_HAS_ORDER, config={"threshold": 1})

    blocked = await service.evaluate(_context())
    other_buyer = await service.evaluate(_context(buyer_id="B-2"))

    assert blocked.decision == DeliveryDecision.BLOCK.value
    assert other_buyer.decision == DeliveryDecision.ALLOW.value


async def test_buyer_has_order_global_spans_accounts(wired_db: Path) -> None:
    await _seed_order("acc-2", "O-9", buyer_id="B-1", status="paid")
    service = DeliveryGuardService()
    await service.upsert_rule(rule_code=RULE_BUYER_HAS_ORDER, config={"threshold": 1})
    await service.upsert_rule(rule_code=RULE_BUYER_HAS_ORDER_GLOBAL, config={"threshold": 1})

    outcome = await service.evaluate(_context())

    assert outcome.decision == DeliveryDecision.BLOCK.value
    assert RULE_BUYER_HAS_ORDER_GLOBAL in outcome.hit_rule_codes()


async def test_buyer_unconfirmed_counts_delivered_orders(wired_db: Path) -> None:
    await _seed_order("acc-1", "O-8", buyer_id="B-1", status="delivered")
    service = DeliveryGuardService()
    await service.upsert_rule(rule_code=RULE_BUYER_UNCONFIRMED, config={"threshold": 1})

    outcome = await service.evaluate(_context())

    assert outcome.decision == DeliveryDecision.BLOCK.value
    assert "未确认收货" in outcome.reason


async def test_credit_rule_asks_for_a_human(wired_db: Path) -> None:
    service = DeliveryGuardService()
    await service.upsert_rule(rule_code=RULE_BUYER_CREDIT_ZERO)

    outcome = await service.evaluate(_context())

    assert outcome.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value
    assert outcome.needs_human is True


async def test_rule_with_broken_config_fails_closed(wired_db: Path) -> None:
    service = DeliveryGuardService()
    await service.upsert_rule(rule_code=RULE_BUYER_HAS_ORDER, config={"threshold": "两个"})

    outcome = await service.evaluate(_context())

    assert outcome.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value
    assert "fail closed" in outcome.reason


async def test_excluded_item_skips_the_rule(wired_db: Path) -> None:
    await _seed_order("acc-1", "O-7", buyer_id="B-1", status="paid")
    service = DeliveryGuardService()
    await service.upsert_rule(
        rule_code=RULE_BUYER_HAS_ORDER, config={"threshold": 1}, excluded_item_ids=("I-1",)
    )

    outcome = await service.evaluate(_context(item_id="I-1"))

    assert outcome.decision == DeliveryDecision.ALLOW.value
    assert (
        await service.evaluate(_context(item_id="I-2"))
    ).decision == DeliveryDecision.BLOCK.value


async def test_account_rule_overrides_global_and_upsert_is_idempotent(wired_db: Path) -> None:
    service = DeliveryGuardService()
    await service.upsert_rule(rule_code=RULE_BUYER_HAS_ORDER, config={"threshold": 1})
    await service.upsert_rule(
        rule_code=RULE_BUYER_HAS_ORDER, account_id="acc-1", config={"threshold": 3}
    )
    await service.upsert_rule(
        rule_code=RULE_BUYER_HAS_ORDER, account_id="acc-1", config={"threshold": 5}
    )

    rules = await service.list_rules(account_id="acc-1")

    assert len(rules) == 2
    assert {rule.scope_key for rule in rules} == {"global", "acc-1"}
    assert [rule.config.get("threshold") for rule in rules if rule.account_id == "acc-1"] == [5]


async def test_rule_delete_and_disable(wired_db: Path) -> None:
    service = DeliveryGuardService()
    rule = await service.upsert_rule(rule_code=RULE_PERSONAL_BLACKLIST)
    await service.add_blacklist(buyer_id="B-1")

    disabled = await service.set_rule_enabled(rule.local_id, enabled=False)
    assert disabled.enabled is False
    assert (await service.evaluate(_context())).decision == DeliveryDecision.ALLOW.value

    await service.delete_rule(rule.local_id)
    assert await service.list_rules() == []
    with pytest.raises(ValueError, match="不存在"):
        await service.delete_rule(rule.local_id)


def test_cli_guard_and_blacklist_flow(wired_db: Path) -> None:
    listed = runner.invoke(app, ["delivery", "guard", "list"])
    assert listed.exit_code == 0
    assert "暂无守卫规则" in listed.output

    created = runner.invoke(
        app,
        [
            "delivery",
            "guard",
            "set",
            "--rule",
            "buyer_has_order",
            "--config",
            "threshold=2",
            "--reason",
            "买家还有未完成订单",
            "--priority",
            "5",
        ],
    )
    assert created.exit_code == 0, created.output
    assert "buyer_has_order" in created.output

    invalid = runner.invoke(app, ["delivery", "guard", "set", "--rule", "not_a_rule"])
    assert invalid.exit_code == 1
    assert "未知守卫规则" in invalid.output

    added = runner.invoke(
        app, ["delivery", "blacklist", "add", "--buyer-id", "B-9", "--reason", "跑单"]
    )
    assert added.exit_code == 0
    assert "已加入" in added.output

    listed_blacklist = runner.invoke(app, ["delivery", "blacklist", "list"])
    assert listed_blacklist.exit_code == 0
    assert "B-9" in listed_blacklist.output

    evaluated = runner.invoke(
        app, ["delivery", "guard", "evaluate", "--account", "acc-1", "--order-id", "O-1"]
    )
    assert evaluated.exit_code == 0
    assert "allow" in evaluated.output

    removed = runner.invoke(app, ["delivery", "blacklist", "remove", "1", "--yes"])
    assert removed.exit_code == 0
    assert "已移除" in removed.output


def test_cli_guard_evaluate_reports_block(wired_db: Path) -> None:
    async def _seed() -> None:
        service = DeliveryGuardService()
        rule = await service.upsert_rule(rule_code=RULE_PERSONAL_BLACKLIST)
        assert isinstance(rule, GuardRule)
        await service.add_blacklist(buyer_id="B-1", reason="拒收")

    asyncio.run(_seed())

    evaluated = runner.invoke(
        app,
        [
            "delivery",
            "guard",
            "evaluate",
            "--account",
            "acc-1",
            "--order-id",
            "O-1",
            "--buyer-id",
            "B-1",
        ],
    )

    assert evaluated.exit_code == 0
    assert "block" in evaluated.output
    assert "拒收" in evaluated.output
