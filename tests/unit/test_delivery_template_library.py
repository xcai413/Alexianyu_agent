"""发货模板库:复用、引用校验与执行链路(参考 Helper delivery-templates)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.application.delivery import (
    DeliveryExecutor,
    DeliveryPolicyService,
    DeliveryRequest,
    DeliveryTemplateService,
    SendOutcome,
)
from xianyu_agent.application.inventory import InventoryImportService, InventoryService
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts
from xianyu_agent.domain.delivery import DeliveryJobState, TemplateError
from xianyu_agent.infrastructure.database.models import Account as AccountRow, Order as OrderRow


@pytest.fixture
async def template_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "templates.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_order() -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == "acc-1").limit(1)
            )
        ).scalar_one()
        session.add(
            OrderRow(
                account_id=account.id,
                order_id="O-1",
                item_id="I-1",
                item_title="测试商品",
                buyer_id="B-1",
                buyer_name="买家",
                quantity=1,
                status="paid",
                amount=9.9,
            )
        )


async def _seed_pool() -> int:
    pool = await InventoryService().create_pool(name="卡密池", type_="data")
    await InventoryImportService().import_units(pool.local_id or 0, ["CARD-1"])
    return pool.local_id or 0


def _sender(collector: list[str]):
    async def send(_account: str, _order: str, text: str) -> SendOutcome:
        collector.append(text)
        return SendOutcome.success(external_message_id="m-1")

    return send


async def test_template_rejects_non_whitelisted_variables(template_db: None) -> None:
    with pytest.raises(TemplateError, match="不在白名单"):
        await DeliveryTemplateService().create(name="坏模板", content="你好 {nickname}")


async def test_template_declares_the_variables_it_uses(template_db: None) -> None:
    template = await DeliveryTemplateService().create(
        name="标准卡密", content="你好 {buyer_name},你的卡密是 {card}"
    )

    assert template.variables == ("buyer_name", "card")
    assert template.enabled is True


async def test_template_name_is_unique(template_db: None) -> None:
    await DeliveryTemplateService().create(name="同名", content="A")

    with pytest.raises(TemplateError, match="已存在"):
        await DeliveryTemplateService().create(name="同名", content="B")


async def test_policy_referencing_a_template_renders_it_at_delivery_time(
    template_db: None,
) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryTemplateService().create(
        name="标准卡密", content="你好 {buyer_name},卡密:{card}"
    )
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", template_ref="标准卡密"
    )
    sent: list[str] = []

    result = await DeliveryExecutor(sender=_sender(sent)).execute(
        DeliveryRequest(account_id="acc-1", order_id="O-1")
    )

    assert result.job.state == DeliveryJobState.COMPLETED.value
    assert sent == ["你好 买家,卡密:CARD-1"]


async def test_disabled_template_blocks_delivery_instead_of_sending_raw_card(
    template_db: None,
) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    service = DeliveryTemplateService()
    template = await service.create(name="标准卡密", content="卡密:{card}")
    assert template.local_id is not None
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", template_ref="标准卡密"
    )
    await service.set_enabled(template.local_id, enabled=False)
    sent: list[str] = []

    with pytest.raises(ValueError, match="已停用"):
        await DeliveryExecutor(sender=_sender(sent)).execute(
            DeliveryRequest(account_id="acc-1", order_id="O-1")
        )

    assert sent == []


async def test_inline_template_wins_over_library_reference(template_db: None) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryTemplateService().create(name="标准卡密", content="库:{card}")
    await DeliveryPolicyService().create(
        name="策略",
        inventory_pool_id=pool_id,
        account_id="acc-1",
        template="内联:{card}",
        template_ref="标准卡密",
    )
    sent: list[str] = []

    await DeliveryExecutor(sender=_sender(sent)).execute(
        DeliveryRequest(account_id="acc-1", order_id="O-1")
    )

    assert sent == ["内联:CARD-1"]


async def test_delete_is_refused_while_an_enabled_policy_uses_the_template(
    template_db: None,
) -> None:
    pool_id = await _seed_pool()
    service = DeliveryTemplateService()
    template = await service.create(name="标准卡密", content="卡密:{card}")
    assert template.local_id is not None
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", template_ref="标准卡密"
    )

    with pytest.raises(ValueError, match="仍被 1 条启用策略引用"):
        await service.delete(template.local_id)

    assert await service.usage_count("标准卡密") == 1
