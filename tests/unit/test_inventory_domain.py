"""库存领域模型的纯逻辑测试(Issue #5 Phase 6)。"""

from __future__ import annotations

import pytest

from xianyu_agent.domain.inventory import (
    InvalidStateTransition,
    InventoryPool,
    InventoryUnit,
    InventoryUnitState,
    ReservationState,
    UnitImportSummary,
    plan_reservation,
    plan_unit_import,
)
from xianyu_agent.domain.inventory.reservation import InsufficientStock, Reservation
from xianyu_agent.domain.inventory.unit import masked_preview


def test_pool_accepts_each_supported_type() -> None:
    pools = (
        InventoryPool(name="文案池", type="text", content="结算后发我地址"),
        InventoryPool(name="卡密池", type="data"),
        InventoryPool(name="图片池", type="image", image_url="https://example.com/a.png"),
    )
    for pool, pool_type in zip(pools, ("text", "data", "image"), strict=True):
        assert pool.is_consumable is (pool_type == "data")
        assert pool.is_literal is (pool_type != "data")

    api_pool = InventoryPool(name="外部发卡", type="api", api_config_ref="api-config-1")
    assert api_pool.api_config_ref == "api-config-1"
    assert api_pool.is_consumable is False
    assert api_pool.is_literal is False


def test_literal_pools_need_content_only_when_enabled() -> None:
    # 停用的池允许先建后填:运营先建池、再补内容,最后才启用。
    draft = InventoryPool(name="文案池", type="text", enabled=False)
    assert draft.content is None
    assert InventoryPool(name="图片池", type="image", enabled=False).image_url is None

    with pytest.raises(ValueError, match="text 类型库存池必须提供发货文案"):
        InventoryPool(name="文案池", type="text")
    with pytest.raises(ValueError, match="image 类型库存池必须提供图片地址"):
        InventoryPool(name="图片池", type="image")
    with pytest.raises(ValueError, match="不允许配置发货文案或图片地址"):
        InventoryPool(name="卡密池", type="data", content="不是卡密的文案")
    with pytest.raises(ValueError, match="不允许配置发货文案或图片地址"):
        InventoryPool(
            name="外部发卡", type="api", api_config_ref="api-1", image_url="https://x/a.png"
        )
    # 内容两端空白只算空内容,不能靠空格过校验。
    with pytest.raises(ValueError, match="text 类型库存池必须提供发货文案"):
        InventoryPool(name="文案池", type="text", content="   ")


def test_pool_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="名称"):
        InventoryPool(name="   ", type="text")
    with pytest.raises(ValueError, match="未知库存池类型"):
        InventoryPool(name="卡密池", type="magic")
    with pytest.raises(ValueError, match="延迟发货秒数"):
        InventoryPool(name="卡密池", type="data", delay_seconds=3601)
    with pytest.raises(ValueError, match="api_config_ref"):
        InventoryPool(name="外部发卡", type="api")
    with pytest.raises(ValueError, match="不允许配置 api_config_ref"):
        InventoryPool(name="文本", type="text", api_config_ref="api-config-1")


def test_pool_delay_boundary_is_inclusive() -> None:
    assert InventoryPool(name="卡密池", type="data", delay_seconds=0).waits_before_delivery is False
    assert (
        InventoryPool(name="卡密池", type="data", delay_seconds=3600).waits_before_delivery is True
    )


def _unit(**kwargs: object) -> InventoryUnit:
    payload: dict[str, object] = {"pool_id": 1, "content": "CARD-0001"}
    payload.update(kwargs)
    return InventoryUnit(**payload)  # type: ignore[arg-type]


def test_unit_requires_consistent_reservation_fields() -> None:
    with pytest.raises(ValueError, match="内容不能为空"):
        _unit(content="  ")
    with pytest.raises(ValueError, match="必须记录占用方"):
        _unit(state=InventoryUnitState.RESERVED.value)
    with pytest.raises(ValueError, match="不应带有占用方"):
        _unit(reserved_by="job-1")


def test_unit_transitions_follow_the_state_machine() -> None:
    unit = _unit(local_id=7)

    reserved = unit.transition_to(InventoryUnitState.RESERVED.value, job_ref="job-1")
    assert reserved.state == InventoryUnitState.RESERVED.value
    assert reserved.reserved_by == "job-1"

    consumed = reserved.transition_to(InventoryUnitState.CONSUMED.value)
    assert consumed.state == InventoryUnitState.CONSUMED.value
    assert consumed.reserved_by == "job-1"

    with pytest.raises(InvalidStateTransition, match="不能从 consumed"):
        consumed.transition_to(InventoryUnitState.AVAILABLE.value)
    with pytest.raises(InvalidStateTransition, match="已经处于 available"):
        unit.transition_to(InventoryUnitState.AVAILABLE.value)
    with pytest.raises(ValueError, match="预留状态必须记录占用方"):
        unit.transition_to(InventoryUnitState.RESERVED.value)


def test_uncertain_units_can_only_be_resolved() -> None:
    unit = _unit(local_id=3, state=InventoryUnitState.UNCERTAIN.value, reserved_by="job-1")

    with pytest.raises(InvalidStateTransition):
        unit.transition_to(InventoryUnitState.RESERVED.value)

    assert unit.transition_to(InventoryUnitState.CONSUMED.value).state == "consumed"
    assert unit.transition_to(InventoryUnitState.AVAILABLE.value).reserved_by is None


def test_reservation_requires_matching_unit_count() -> None:
    with pytest.raises(ValueError, match="预留数量至少为 1"):
        Reservation(pool_id=1, job_ref="job-1", quantity=0)
    with pytest.raises(ValueError, match="预留单元数与预留数量不一致"):
        Reservation(pool_id=1, job_ref="job-1", quantity=2, unit_ids=(1,))
    with pytest.raises(ValueError, match="job_ref"):
        Reservation(pool_id=1, job_ref="  ", quantity=1)


def test_reservation_state_machine_and_terminal_states() -> None:
    reservation = Reservation(pool_id=1, job_ref="job-1", quantity=1, unit_ids=(5,))

    assert reservation.is_terminal is False
    uncertain = reservation.transition_to(ReservationState.UNCERTAIN.value)
    assert uncertain.transition_to(ReservationState.CONSUMED.value).is_terminal is True
    with pytest.raises(InvalidStateTransition):
        reservation.transition_to(ReservationState.RESERVED.value)


def test_plan_reservation_is_all_or_nothing() -> None:
    assert plan_reservation([11, 12, 13], 2) == (11, 12)

    with pytest.raises(InsufficientStock, match="可用单元 1 条"):
        plan_reservation([11], 2)
    with pytest.raises(ValueError, match="至少为 1"):
        plan_reservation([11], 0)


def test_plan_unit_import_skips_blanks_and_duplicates() -> None:
    accepted, summary = plan_unit_import(
        [" CARD-1 ", "CARD-2", "", "card-1", "CARD-1", "  ", "CARD-3"],
        ["CARD-2"],
    )

    # 卡密大小写敏感:"card-1" 与 "CARD-1" 是两条不同的卡密。
    assert accepted == ("CARD-1", "card-1", "CARD-3")
    assert summary == UnitImportSummary(added=3, skipped_duplicates=2, skipped_blank=2)
    assert summary.seen == 7


def test_masked_preview_hides_the_middle() -> None:
    assert masked_preview("ABCDEFGH") == "AB***GH"
    assert masked_preview("AB") == "**"
    assert masked_preview("   ") == ""
