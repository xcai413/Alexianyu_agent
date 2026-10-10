"""发货执行器:校验、守卫、预留、发送与收尾(Issue #5 Phase 6, 6G)。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from xianyu_agent.application.delivery import (
    ConsignOutcome,
    DeliveryExecutor,
    DeliveryGuardService,
    DeliveryJobService,
    DeliveryPolicyService,
    DeliveryRequest,
    SendOutcome,
)
from xianyu_agent.application.delivery.recovery_service import DeliveryRecoveryService
from xianyu_agent.application.inventory import (
    InventoryImportService,
    InventoryQueryService,
    InventoryReservationService,
    InventoryService,
)
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.domain.delivery import (
    RULE_BUYER_HAS_ORDER,
    RULE_PERSONAL_BLACKLIST,
    DeliveryDecision,
    DeliveryJobState,
    FailureKind,
)
from xianyu_agent.domain.item.sku import normalize_specs
from xianyu_agent.domain.item.sku_binding import POOL_TARGET, SpecBinding, upsert_item_binding
from xianyu_agent.infrastructure.database.models import Account as AccountRow, Order as OrderRow
from xianyu_agent.protocol.items_client import RemoteItem

RED_M = {"颜色": "红色", "尺码": "M"}


@pytest.fixture
async def delivery_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "executor.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-1", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


async def _seed_order(
    order_id: str = "O-1",
    *,
    status: str = "paid",
    buyer_id: str = "B-1",
    quantity: int = 1,
    item_id: str = "I-1",
    account_id: str = "acc-1",
) -> None:
    async with db_mod.async_session_factory() as session, session.begin():
        account = (
            await session.execute(
                select(AccountRow).where(AccountRow.account_id == account_id).limit(1)
            )
        ).scalar_one()
        session.add(
            OrderRow(
                account_id=account.id,
                order_id=order_id,
                item_id=item_id,
                item_title="多规格商品",
                buyer_id=buyer_id,
                buyer_name="买家",
                quantity=quantity,
                status=status,
                amount=45.0,
            )
        )


async def _seed_item(item_id: str = "I-1") -> None:
    """规格绑定挂在本地镜像商品上,所以要先有一条商品。"""
    await domain_items.apply_on_sale_snapshot(
        "acc-1",
        [
            RemoteItem(
                item_id=item_id,
                title="多规格商品",
                price="45",
                status="1",
                detail_url=None,
                main_image_url=None,
                category_id=None,
                auction_type=None,
                raw_payload=None,
            )
        ],
    )


async def _seed_pool(units: tuple[str, ...] = ("CARD-1", "CARD-2")) -> int:
    pool = await InventoryService().create_pool(name="卡密池", type_="data")
    await InventoryImportService().import_units(pool.local_id, list(units))
    return pool.local_id


async def _seed_text_pool(content: str = "结算后把收货地址发我", *, enabled: bool = True) -> int:
    pool = await InventoryService().create_pool(
        name="文案池", type_="text", content=content, enabled=enabled
    )
    return pool.local_id


async def _seed_image_pool(image_url: str = "https://example.com/card.png") -> int:
    pool = await InventoryService().create_pool(name="图片池", type_="image", image_url=image_url)
    return pool.local_id


async def _pool_counts(pool_id: int) -> dict[str, int]:
    view = await InventoryQueryService().pool_detail(pool_id)
    return dict(view.counts)


def _request(**kwargs: object) -> DeliveryRequest:
    payload: dict[str, object] = {"account_id": "acc-1", "order_id": "O-1"}
    payload.update(kwargs)
    return DeliveryRequest(**payload)  # type: ignore[arg-type]


def _sender_returning(sent: bool, *, failure: FailureKind | None = None):
    async def sender(_account: str, _order: str, _text: str) -> SendOutcome:
        if sent:
            return SendOutcome.success(external_message_id="msg-1")
        return SendOutcome(sent=False, failure=failure, error="发送失败")

    return sender


async def test_plan_reports_order_state_and_policy(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(
        name="账号策略", inventory_pool_id=pool_id, account_id="acc-1"
    )

    plan = await DeliveryExecutor().plan(_request())

    assert plan.allowed is True
    assert plan.pool_id == pool_id
    assert plan.quantity == 1
    assert plan.policy_level == "account"


async def test_plan_blocks_unknown_order_and_delivered_order(delivery_db) -> None:
    executor = DeliveryExecutor()

    missing = await executor.plan(_request(order_id="O-404"))
    assert missing.allowed is False
    assert "不在本地镜像" in missing.reason

    await _seed_order("O-2", status="delivered")
    delivered = await executor.plan(_request(order_id="O-2"))
    assert delivered.allowed is False
    assert "已发货" in delivered.reason


async def test_plan_reports_guard_block(delivery_db) -> None:
    await _seed_order()
    guards = DeliveryGuardService()
    await guards.upsert_rule(rule_code=RULE_PERSONAL_BLACKLIST)
    await guards.add_blacklist(buyer_id="B-1", reason="跑单")

    plan = await DeliveryExecutor().plan(_request())

    assert plan.allowed is False
    assert plan.guard_decision == DeliveryDecision.BLOCK.value
    assert plan.hit_rules == (RULE_PERSONAL_BLACKLIST,)


async def test_execute_happy_path_consumes_inventory_and_marks_order(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")
    sent_texts: list[str] = []

    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent_texts.append(text)
        return SendOutcome.success(external_message_id="msg-1")

    result = await DeliveryExecutor(sender=sender).execute(_request())

    assert result.decision == DeliveryDecision.ALLOW.value
    assert result.job.state == DeliveryJobState.COMPLETED.value
    assert result.contents_sent == 1
    assert sent_texts == ["CARD-1"]
    assert (await _pool_counts(pool_id))["consumed"] == 1
    async with db_mod.async_session_factory() as session:
        order = (
            await session.execute(select(OrderRow).where(OrderRow.order_id == "O-1"))
        ).scalar_one()
    assert order.status == "delivered"
    assert order.delivered_at is not None
    attempts = await DeliveryJobService().attempts(result.job.local_id)
    assert [attempt.action for attempt in attempts] == ["reserve", "send"]


async def test_multi_quantity_reserves_and_sends_each_unit(delivery_db) -> None:
    await _seed_order(quantity=2)
    pool_id = await _seed_pool(units=("A-1", "A-2"))
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")
    sent_texts: list[str] = []

    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent_texts.append(text)
        return SendOutcome.success()

    result = await DeliveryExecutor(sender=sender).execute(_request())

    assert result.contents_sent == 2
    assert sorted(sent_texts) == ["A-1", "A-2"]
    assert (await _pool_counts(pool_id))["consumed"] == 2


async def test_text_pool_sends_its_content_without_reserving_anything(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_text_pool("结算后把收货地址发我")
    await DeliveryPolicyService().create(
        name="文案策略", inventory_pool_id=pool_id, account_id="acc-1"
    )
    sent_texts: list[str] = []

    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent_texts.append(text)
        return SendOutcome.success(external_message_id="msg-text")

    result = await DeliveryExecutor(sender=sender).execute(_request())

    assert result.decision == DeliveryDecision.ALLOW.value
    assert result.job.state == DeliveryJobState.COMPLETED.value
    assert sent_texts == ["结算后把收货地址发我"]
    assert result.contents_sent == 1
    counts = await _pool_counts(pool_id)
    assert counts["total"] == 0
    assert counts["reserved"] == 0
    attempts = await DeliveryJobService().attempts(result.job.local_id)
    assert [attempt.action for attempt in attempts] == ["send"]


async def test_text_pool_sends_once_even_when_the_order_asks_for_two(delivery_db) -> None:
    await _seed_order(quantity=2)
    pool_id = await _seed_text_pool()
    await DeliveryPolicyService().create(
        name="文案策略", inventory_pool_id=pool_id, account_id="acc-1"
    )
    sent_texts: list[str] = []

    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent_texts.append(text)
        return SendOutcome.success()

    executor = DeliveryExecutor(sender=sender)
    plan = await executor.plan(_request())
    result = await executor.execute(_request())

    # 同一句话重复发给买家没有意义:数量只对 data / api 池表示要取几份。
    assert plan.quantity == 1
    assert result.contents_sent == 1
    assert sent_texts == ["结算后把收货地址发我"]


async def test_text_pool_template_wraps_the_pool_content(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_text_pool("兑换码 A-1")
    await DeliveryPolicyService().create(
        name="文案策略",
        inventory_pool_id=pool_id,
        account_id="acc-1",
        template="尊敬的 {buyer_name}:{card}",
    )
    sent_texts: list[str] = []

    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent_texts.append(text)
        return SendOutcome.success()

    await DeliveryExecutor(sender=sender).execute(_request())

    assert sent_texts == ["尊敬的 买家:兑换码 A-1"]


async def test_disabled_text_pool_blocks_delivery(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_text_pool("结算后把收货地址发我", enabled=False)
    await DeliveryPolicyService().create(
        name="文案策略", inventory_pool_id=pool_id, account_id="acc-1"
    )
    sent_texts: list[str] = []

    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent_texts.append(text)
        return SendOutcome.success()

    result = await DeliveryExecutor(sender=sender).execute(_request())

    assert result.decision == DeliveryDecision.BLOCK.value
    assert result.job.state == DeliveryJobState.BLOCKED.value
    assert "已停用" in result.reason
    assert sent_texts == []


async def test_image_pool_blocks_until_image_sending_exists(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_image_pool()
    await DeliveryPolicyService().create(
        name="图片策略", inventory_pool_id=pool_id, account_id="acc-1"
    )
    sent_texts: list[str] = []

    async def sender(_account: str, _order: str, text: str) -> SendOutcome:
        sent_texts.append(text)
        return SendOutcome.success()

    result = await DeliveryExecutor(sender=sender).execute(_request())

    # 图片地址不能当文本发出去,否则买家收到一条链接;没有注入图片端口时必须停下。
    assert result.decision == DeliveryDecision.BLOCK.value
    assert result.job.state == DeliveryJobState.BLOCKED.value
    assert "图片发送端口" in result.reason
    assert sent_texts == []


async def test_image_pool_uses_the_image_sender_when_injected(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_image_pool()
    await DeliveryPolicyService().create(
        name="图片策略", inventory_pool_id=pool_id, account_id="acc-1"
    )
    sent_images: list[tuple[str, str, str]] = []

    async def image_sender(account: str, receiver: str, image_url: str) -> SendOutcome:
        sent_images.append((account, receiver, image_url))
        return SendOutcome.success(external_message_id="img-1")

    result = await DeliveryExecutor(image_sender=image_sender).execute(_request())

    assert result.decision == DeliveryDecision.ALLOW.value
    assert result.job.state == DeliveryJobState.COMPLETED.value
    assert sent_images == [
        ("acc-1", "B-1", "https://example.com/card.png")
    ]


async def test_image_pool_reports_unknown_outcome_as_uncertain(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_image_pool()
    await DeliveryPolicyService().create(
        name="图片策略", inventory_pool_id=pool_id, account_id="acc-1"
    )

    async def image_sender(_account: str, _receiver: str, _url: str) -> SendOutcome:
        return SendOutcome.unknown("图片通道返回失败,无法确认对方是否收到")

    result = await DeliveryExecutor(image_sender=image_sender).execute(_request())

    assert result.job.state == DeliveryJobState.UNCERTAIN.value
    assert result.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value


async def test_pool_account_scope_is_enforced_at_delivery_time(delivery_db) -> None:
    await _seed_order()
    pool = await InventoryService().create_pool(
        name="别人账号的池", type_="data", account_scope="acc-2"
    )
    await InventoryImportService().import_units(pool.local_id or 0, ["CARD-1"])
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool.local_id or 0, account_id="acc-1"
    )
    sent: list[str] = []

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(_request())

    # 池限定的是别的账号:必须直接拒绝,而不是把别人的库存发给这个账号的买家。
    assert result.decision == DeliveryDecision.BLOCK.value
    assert result.job.state == DeliveryJobState.BLOCKED.value
    assert "限定账号 acc-2" in result.reason
    assert sent == []
    plan = await DeliveryExecutor().plan(_request())
    assert plan.allowed is False
    assert "限定账号 acc-2" in plan.reason


async def test_pool_item_scope_is_enforced_at_delivery_time(delivery_db) -> None:
    await _seed_order()
    pool = await InventoryService().create_pool(
        name="别的商品池", type_="data", item_scope="I-OTHER"
    )
    await InventoryImportService().import_units(pool.local_id or 0, ["CARD-1"])
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool.local_id or 0, account_id="acc-1"
    )

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(_request())

    assert result.decision == DeliveryDecision.BLOCK.value
    assert "限定商品 I-OTHER" in result.reason


async def test_pool_scope_matching_the_order_is_allowed(delivery_db) -> None:
    await _seed_order()
    pool = await InventoryService().create_pool(
        name="本商品池", type_="data", account_scope="acc-1", item_scope="I-1"
    )
    await InventoryImportService().import_units(pool.local_id or 0, ["CARD-1"])
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool.local_id or 0, account_id="acc-1"
    )

    plan = await DeliveryExecutor().plan(_request())
    assert plan.allowed is True
    assert plan.pool_id == pool.local_id

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(_request())

    assert result.decision == DeliveryDecision.ALLOW.value
    assert result.job.state == DeliveryJobState.COMPLETED.value


async def test_pool_delay_is_effective_and_takes_the_larger_value(delivery_db) -> None:
    await _seed_order()
    pool = await InventoryService().create_pool(
        name="慢接口池", type_="data", delay_seconds=30
    )
    await InventoryImportService().import_units(pool.local_id or 0, ["CARD-1"])
    await DeliveryPolicyService().create(
        name="无延迟策略", inventory_pool_id=pool.local_id or 0, account_id="acc-1"
    )
    waited: list[float] = []

    async def waiter(seconds: float) -> None:
        waited.append(seconds)

    # Dry Run 要先跑:执行成功会把订单镜像标成已发货,之后 plan 自然不再放行。
    plan = await DeliveryExecutor().plan(_request())
    assert plan.delay_seconds == 30
    result = await DeliveryExecutor(sender=_sender_returning(True), waiter=waiter).execute(
        _request()
    )

    # 池级 30s、策略级 0s:生效的是较大者,不是相加也不是忽略。
    assert waited == [30.0]
    assert result.decision == DeliveryDecision.ALLOW.value


async def test_policy_delay_wins_when_it_is_larger(delivery_db) -> None:
    await _seed_order()
    pool = await InventoryService().create_pool(name="普通池", type_="data", delay_seconds=5)
    await InventoryImportService().import_units(pool.local_id or 0, ["CARD-1"])
    await DeliveryPolicyService().create(
        name="慢策略",
        inventory_pool_id=pool.local_id or 0,
        account_id="acc-1",
        delay_seconds=45,
    )
    waited: list[float] = []

    async def waiter(seconds: float) -> None:
        waited.append(seconds)

    await DeliveryExecutor(sender=_sender_returning(True), waiter=waiter).execute(_request())

    assert waited == [45.0]


async def test_not_sent_failure_releases_inventory(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")

    result = await DeliveryExecutor(
        sender=_sender_returning(False, failure=FailureKind.NOT_SENT)
    ).execute(_request())

    assert result.job.state == DeliveryJobState.FAILED_RETRYABLE.value
    counts = await _pool_counts(pool_id)
    assert counts["available"] == 2
    assert counts["reserved"] == 0
    async with db_mod.async_session_factory() as session:
        order = (
            await session.execute(select(OrderRow).where(OrderRow.order_id == "O-1"))
        ).scalar_one()
    assert order.status == "paid"
    assert order.delivery_fail_reason == "发送失败"


async def test_unknown_failure_freezes_inventory_and_job(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")

    result = await DeliveryExecutor(
        sender=_sender_returning(False, failure=FailureKind.UNKNOWN)
    ).execute(_request())

    assert result.job.state == DeliveryJobState.UNCERTAIN.value
    counts = await _pool_counts(pool_id)
    assert counts["uncertain"] == 1
    assert counts["available"] == 1


async def test_guard_block_stops_before_reserving(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")
    guards = DeliveryGuardService()
    await guards.upsert_rule(rule_code=RULE_BUYER_HAS_ORDER, config={"threshold": 1})
    await _seed_order("O-0", buyer_id="B-1")

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(_request())

    assert result.decision == DeliveryDecision.BLOCK.value
    assert result.job.state == DeliveryJobState.BLOCKED.value
    assert (await _pool_counts(pool_id))["available"] == 2


async def test_missing_policy_blocks_delivery(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(_request())

    assert result.decision == DeliveryDecision.BLOCK.value
    assert "没有任何启用的策略" in result.reason
    assert (await _pool_counts(pool_id))["available"] == 2


async def test_without_sender_nothing_is_reserved(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")

    result = await DeliveryExecutor().execute(_request())

    assert result.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value
    assert (await _pool_counts(pool_id))["available"] == 2


async def test_delay_is_awaited_before_sending(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", delay_seconds=30
    )
    waited: list[float] = []

    async def waiter(seconds: float) -> None:
        waited.append(seconds)

    result = await DeliveryExecutor(sender=_sender_returning(True), waiter=waiter).execute(
        _request()
    )

    assert waited == [30.0]
    assert result.job.state == DeliveryJobState.COMPLETED.value


async def test_platform_confirm_stops_at_confirming(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", platform_confirm=True
    )

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(_request())

    assert result.job.state == DeliveryJobState.CONFIRMING.value
    assert result.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value
    counts = await _pool_counts(pool_id)
    assert counts["reserved"] == 1
    assert counts["consumed"] == 0


def _consign_returning(outcome: ConsignOutcome, seen: list[tuple[str, str, str]]):
    async def consign(account_id: str, order_id: str, credential: str) -> ConsignOutcome:
        seen.append((account_id, order_id, credential))
        return outcome

    return consign


async def test_platform_confirm_success_completes_and_consumes_inventory(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool(units=("CARD-1",))
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", platform_confirm=True
    )
    seen: list[tuple[str, str, str]] = []

    result = await DeliveryExecutor(
        sender=_sender_returning(True),
        consign=_consign_returning(ConsignOutcome.success(), seen),
    ).execute(_request())

    assert result.job.state == DeliveryJobState.COMPLETED.value
    assert result.decision == DeliveryDecision.ALLOW.value
    assert "已确认平台发货" in result.reason
    # 平台确认时带回了真实发给买家的凭证,账号与订单必须对得上。
    assert seen == [("acc-1", "O-1", "CARD-1")]
    counts = await _pool_counts(pool_id)
    assert counts["consumed"] == 1


async def test_platform_confirm_rejection_freezes_inventory_and_needs_human(
    delivery_db,
) -> None:
    await _seed_order()
    pool_id = await _seed_pool(units=("CARD-1",))
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", platform_confirm=True
    )

    result = await DeliveryExecutor(
        sender=_sender_returning(True),
        consign=_consign_returning(
            ConsignOutcome.rejected("确认发货被拒绝: 订单状态错误"), []
        ),
    ).execute(_request())

    assert result.job.state == DeliveryJobState.FAILED_FINAL.value
    assert result.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value
    assert "平台确认发货未完成" in result.reason
    counts = await _pool_counts(pool_id)
    # 卡密已经发给买家,绝不能退回库存被第二个买家拿到。
    assert counts["consumed"] == 0
    assert counts["reserved"] == 1


async def test_platform_confirm_unknown_outcome_goes_uncertain(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool(units=("CARD-1",))
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", platform_confirm=True
    )

    result = await DeliveryExecutor(
        sender=_sender_returning(True),
        consign=_consign_returning(ConsignOutcome.unknown("确认发货网络请求失败"), []),
    ).execute(_request())

    assert result.job.state == DeliveryJobState.UNCERTAIN.value
    assert result.decision == DeliveryDecision.REQUIRE_CONFIRMATION.value


async def test_platform_confirm_port_exception_is_never_treated_as_confirmed(
    delivery_db,
) -> None:
    await _seed_order()
    pool_id = await _seed_pool(units=("CARD-1",))
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=pool_id, account_id="acc-1", platform_confirm=True
    )

    async def exploding(_account: str, _order: str, _credential: str) -> ConsignOutcome:
        raise RuntimeError("boom")

    result = await DeliveryExecutor(
        sender=_sender_returning(True), consign=exploding
    ).execute(_request())

    assert result.job.state == DeliveryJobState.UNCERTAIN.value
    counts = await _pool_counts(pool_id)
    assert counts["consumed"] == 0


async def test_spec_binding_overrides_policy_pool(delivery_db) -> None:
    await _seed_order()
    await _seed_item()
    policy_pool = await _seed_pool(units=("P-1",))
    spec_pool = await _seed_pool(units=("S-1", "S-2"))
    await DeliveryPolicyService().create(
        name="策略", inventory_pool_id=policy_pool, account_id="acc-1"
    )
    await upsert_item_binding(
        "acc-1",
        "I-1",
        SpecBinding(
            specs=normalize_specs(RED_M), target_kind=POOL_TARGET, target_id=spec_pool, quantity=2
        ),
    )

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(
        _request(selection=normalize_specs(RED_M))
    )

    assert result.contents_sent == 2
    assert (await _pool_counts(spec_pool))["consumed"] == 2
    assert (await _pool_counts(policy_pool))["available"] == 1


async def test_recovery_scans_and_freezes_in_flight_jobs(delivery_db) -> None:
    await _seed_order()
    pool_id = await _seed_pool()
    await DeliveryPolicyService().create(name="策略", inventory_pool_id=pool_id, account_id="acc-1")
    jobs = DeliveryJobService()
    job = await jobs.create_or_get(account_id="acc-1", order_id="O-1")
    await jobs.set_state(job.local_id, DeliveryJobState.CHECKING.value)
    await jobs.set_state(job.local_id, DeliveryJobState.RESERVING.value)
    reservation = await InventoryReservationService().reserve(pool_id, "job", 1)
    await jobs.attach_reservation(job.local_id, reservation.local_id)
    await jobs.set_state(job.local_id, DeliveryJobState.RESERVED.value)
    await jobs.set_state(job.local_id, DeliveryJobState.DELIVERING.value)

    recovery = DeliveryRecoveryService()
    scanned = await recovery.scan(account_id="acc-1")
    assert [item.action for item in scanned.items] == ["freeze"]

    applied = await recovery.freeze_in_flight(account_id="acc-1")

    assert applied.frozen == (job.local_id,)
    assert (await jobs.get(job.local_id)).state == DeliveryJobState.UNCERTAIN.value
    assert (await _pool_counts(pool_id))["uncertain"] == 1


async def test_unknown_order_blocks_cleanly_instead_of_raising(delivery_db) -> None:
    """订单不在镜像里是最常见的拒绝路径:它必须落成 blocked,而不是抛状态机异常。

    2026-09-21 的真实缺陷:执行器先判断"不放行"再切 checking,于是 pending → blocked 这个
    非法转移会抛 InvalidStateTransition,CLI 直接吐堆栈、任务还留在 pending。
    """
    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(
        _request(order_id="O-404")
    )

    assert result.decision == DeliveryDecision.BLOCK.value
    assert result.job.state == DeliveryJobState.BLOCKED.value
    assert "不在本地镜像" in result.reason
    assert result.job.last_error == result.reason


async def test_unpaid_order_blocks_cleanly(delivery_db) -> None:
    await _seed_order(status="pending_payment")

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(_request())

    assert result.decision == DeliveryDecision.BLOCK.value
    assert result.job.state == DeliveryJobState.BLOCKED.value
    assert "还不是已付款" in result.reason


async def test_rerunning_a_blocked_order_rechecks_instead_of_failing(delivery_db) -> None:
    """blocked 只是"这次没放行",修好原因后应该能显式再跑一次(blocked → checking → blocked)。"""
    executor = DeliveryExecutor(sender=_sender_returning(True))
    first = await executor.execute(_request(order_id="O-404"))

    again = await DeliveryExecutor(sender=_sender_returning(True)).execute(
        _request(order_id="O-404")
    )

    assert first.job.local_id == again.job.local_id
    assert again.decision == DeliveryDecision.BLOCK.value
    assert again.job.state == DeliveryJobState.BLOCKED.value


async def test_already_delivered_order_blocks_without_raising(delivery_db) -> None:
    await _seed_order(status="delivered")

    result = await DeliveryExecutor(sender=_sender_returning(True)).execute(_request())

    assert result.decision == DeliveryDecision.BLOCK.value
    assert result.job.state == DeliveryJobState.BLOCKED.value
    assert "不重复履约" in result.reason
