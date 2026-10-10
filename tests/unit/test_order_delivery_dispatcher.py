"""OrderPaid 分发:阶段 6 引擎与旧卡密链路的选择(Issue #5 Phase 6)。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.application.delivery import (
    DeliveryExecutor,
    DeliveryPolicyService,
    OrderDeliveryDispatcher,
    SendOutcome,
)
from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryService,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.events import OrderPaid
from xianyu_agent.infrastructure.database.models import Account as AccountRow, Order as OrderRow


@pytest.fixture
async def dispatcher_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "dispatcher.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _event(order_id: str = "O-1") -> OrderPaid:
    return OrderPaid(
        event_id="evt-1",
        account_id="acc-1",
        order_id=order_id,
        item_id="I-1",
        item_title="多规格商品",
        buyer_id="B-1",
        buyer_name="买家",
        amount=45.0,
        received_at=datetime.now(UTC),
    )


@dataclass
class _LegacyResult:
    delivered: bool


async def test_auto_engine_uses_legacy_when_no_policy(dispatcher_db) -> None:
    calls: list[str] = []

    async def raw_sender(_account: str, _order: str, _text: str) -> bool:
        calls.append("sent")
        return True

    async def legacy(event: OrderPaid) -> object:
        calls.append(f"legacy:{event.order_id}")
        return _LegacyResult(delivered=True)

    dispatcher = OrderDeliveryDispatcher(sender=raw_sender, legacy_factory=lambda: legacy)

    outcome = await dispatcher.deliver(_event())

    assert outcome == "legacy_delivered"
    assert calls == ["legacy:O-1"]


async def test_auto_engine_uses_phase6_when_policy_exists(dispatcher_db) -> None:
    pool = await InventoryService().create_pool(name="卡密池", type_="data")
    await InventoryImportService().import_units(pool.local_id, ["CARD-1"])
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool.local_id, account_id="acc-1"
    )

    sent: list[str] = []

    async def raw_sender(_account: str, order_id: str, text: str) -> bool:
        sent.append(text)
        return True

    async def legacy(_event: OrderPaid) -> object:  # pragma: no cover - 不应被调用
        raise AssertionError("有策略时不应该走旧链路")

    async with db_mod.async_session_factory() as session, session.begin():
        account = (
            await session.execute(select(AccountRow).where(AccountRow.account_id == "acc-1"))
        ).scalar_one()
        session.add(
            OrderRow(
                account_id=account.id,
                order_id="O-1",
                item_id="I-1",
                item_title="多规格商品",
                buyer_id="B-1",
                buyer_name="买家",
                quantity=1,
                status="paid",
                amount=45.0,
            )
        )

    dispatcher = OrderDeliveryDispatcher(
        sender=raw_sender,
        executor=DeliveryExecutor(sender=_collector(sent)),
        legacy_factory=lambda: legacy,
    )

    outcome = await dispatcher.deliver(_event())

    assert outcome == "allow"


async def test_forced_engines_bypass_the_auto_choice(dispatcher_db) -> None:
    async def raw_sender(_account: str, _order: str, _text: str) -> bool:
        return True

    async def legacy(_event: OrderPaid) -> object:
        return _LegacyResult(delivered=False)

    legacy_only = OrderDeliveryDispatcher(
        sender=raw_sender, engine="legacy", legacy_factory=lambda: legacy
    )

    assert await legacy_only.deliver(_event()) == "legacy_failed"
    with pytest.raises(ValueError, match="未知发货引擎"):
        OrderDeliveryDispatcher(sender=raw_sender, engine="magic")


async def test_sender_port_treats_false_as_unknown() -> None:
    async def raw_sender(_account: str, _order: str, _text: str) -> bool:
        return False

    dispatcher = OrderDeliveryDispatcher(sender=raw_sender)
    port = dispatcher._sender_port()

    outcome = await port("acc-1", "O-1", "CARD-1")

    assert outcome.sent is False
    assert outcome.failure.value == "unknown"


async def test_sender_port_maps_exception_to_unknown() -> None:
    async def raw_sender(_account: str, _order: str, _text: str) -> bool:
        raise RuntimeError("boom")

    dispatcher = OrderDeliveryDispatcher(sender=raw_sender)
    port = dispatcher._sender_port()

    outcome = await port("acc-1", "O-1", "CARD-1")

    assert outcome.failure.value == "unknown"
    assert "RuntimeError" in (outcome.error or "")


def _collector(sent: list[str]):
    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent.append(text)
        return SendOutcome.success()

    return sender
