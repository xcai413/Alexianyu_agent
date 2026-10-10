"""Tests for spec -> inventory source bindings (Issue #4 M10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.domain.item.sku import normalize_specs
from xianyu_agent.domain.item.sku_binding import (
    SpecBinding,
    binding_key,
    build_binding_index,
    list_item_bindings,
    remove_item_binding,
    replace_item_bindings,
    resolve_binding,
    resolve_item_binding,
    upsert_item_binding,
    validate_bindings,
)
from xianyu_agent.infrastructure.database.models.item import ItemSpecBinding
from xianyu_agent.protocol.items_client import RemoteItem

RED_M = {"颜色": "红色", "尺码": "M"}
BLUE_M = {"颜色": "蓝色", "尺码": "M"}


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(tmp_path / "bindings.db"))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _binding(
    selection: dict[str, str], *, target: int = 7, quantity: int = 1, enabled: bool = True
):
    return SpecBinding(
        specs=normalize_specs(selection), target_id=target, quantity=quantity, enabled=enabled
    )


async def _seed_item(item_id: str = "I-1") -> None:
    await domain_accounts.create_account("acc-1", enabled=True)
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


def test_binding_validation_rejects_impossible_configuration() -> None:
    with pytest.raises(ValueError, match="至少包含一个规格"):
        SpecBinding(specs=(), target_id=1)
    with pytest.raises(ValueError, match="不能重复"):
        SpecBinding(
            specs=normalize_specs({"颜色": "红色"}),
            target_id=1,
        ) and SpecBinding(
            specs=(normalize_specs({"颜色": "红色"})[0], normalize_specs({"颜色": "蓝色"})[0]),
            target_id=1,
        )
    with pytest.raises(ValueError, match="未知库存来源类型"):
        SpecBinding(specs=normalize_specs(RED_M), target_id=1, target_kind="warehouse")
    with pytest.raises(ValueError, match="正整数"):
        SpecBinding(specs=normalize_specs(RED_M), target_id=0)
    with pytest.raises(ValueError, match="至少为 1"):
        SpecBinding(specs=normalize_specs(RED_M), target_id=1, quantity=0)


def test_binding_key_is_stable_and_unambiguous() -> None:
    first = normalize_specs({"颜色": "红色", "尺码": "M"})
    second = normalize_specs({"尺码": "M", "颜色": "红色"})

    assert binding_key(first) == binding_key(second)
    assert binding_key(first) != binding_key(normalize_specs({"颜色": "红|色=M", "尺码": "M"}))


def test_duplicate_bindings_are_rejected() -> None:
    bindings = (_binding(RED_M), _binding(RED_M, target=9))

    problems = validate_bindings(bindings)

    assert len(problems) == 1
    assert "重复配置" in problems[0]
    with pytest.raises(ValueError, match="重复配置"):
        build_binding_index(bindings)


def test_resolution_requires_an_exact_spec_match() -> None:
    bindings = (_binding(RED_M, target=7, quantity=2), _binding(BLUE_M, target=8, enabled=False))

    hit = resolve_binding(bindings, RED_M)
    disabled = resolve_binding(bindings, BLUE_M)
    unknown = resolve_binding(bindings, {"颜色": "绿色", "尺码": "M"})
    partial = resolve_binding(bindings, {"颜色": "红色"})

    assert hit.matched is True
    assert hit.binding is not None
    assert (hit.binding.target_id, hit.binding.quantity) == (7, 2)
    assert disabled.matched is False
    assert disabled.reason == "该规格组合的库存来源已停用"
    assert unknown.matched is False
    assert unknown.reason == "该规格组合未配置库存来源"
    assert partial.matched is False


@pytest.mark.asyncio
async def test_bindings_round_trip_and_stale_rows_are_removed(clean_db) -> None:
    await _seed_item()
    bindings = (_binding(RED_M, target=7, quantity=2), _binding(BLUE_M, target=8))

    written = await replace_item_bindings("acc-1", "I-1", bindings)
    listed = await list_item_bindings("acc-1", "I-1")

    assert written == 2
    assert [binding.target_id for binding in listed] == [7, 8]
    assert listed[0].quantity == 2
    assert listed[0].describe() == "颜色:红色 尺码:M"

    replaced = await replace_item_bindings("acc-1", "I-1", (_binding(RED_M, target=9),))
    after = await list_item_bindings("acc-1", "I-1")

    assert replaced == 1
    assert [binding.target_id for binding in after] == [9]


@pytest.mark.asyncio
async def test_resolve_item_binding_reads_the_stored_mapping(clean_db) -> None:
    await _seed_item()
    await replace_item_bindings("acc-1", "I-1", (_binding(RED_M, target=7, quantity=3),))

    hit = await resolve_item_binding("acc-1", "I-1", RED_M)
    miss = await resolve_item_binding("acc-1", "I-1", BLUE_M)
    unknown_item = await resolve_item_binding("acc-1", "I-MISSING", RED_M)

    assert hit is not None
    assert hit.binding is not None
    assert hit.binding.target_id == 7
    assert miss is not None
    assert miss.matched is False
    assert unknown_item is None


@pytest.mark.asyncio
async def test_replacing_bindings_rejects_unknown_items_and_keeps_uniqueness(clean_db) -> None:
    await _seed_item()
    with pytest.raises(ValueError, match="不存在"):
        await replace_item_bindings("acc-1", "I-MISSING", (_binding(RED_M),))

    await replace_item_bindings("acc-1", "I-1", (_binding(RED_M),))
    item_pk = (await domain_items.get_item_by_external_id("acc-1", "I-1")).id

    async def insert_duplicate() -> None:
        async with db_mod.get_async_session() as session:
            session.add(
                ItemSpecBinding(
                    item_pk=item_pk,
                    spec_key=binding_key(normalize_specs(RED_M)),
                    specs=[],
                    target_id=1,
                    quantity=1,
                    enabled=True,
                )
            )
            await session.commit()

    with pytest.raises(IntegrityError):
        await insert_duplicate()


@pytest.mark.asyncio
async def test_upsert_and_remove_touch_only_the_named_combination(clean_db) -> None:
    await _seed_item()
    await replace_item_bindings("acc-1", "I-1", (_binding(RED_M, target=7),))

    after_add = await upsert_item_binding("acc-1", "I-1", _binding(BLUE_M, target=8))
    after_replace = await upsert_item_binding("acc-1", "I-1", _binding(RED_M, target=9, quantity=3))
    after_remove = await remove_item_binding("acc-1", "I-1", RED_M)

    assert sorted(binding.target_id for binding in after_add) == [7, 8]
    assert sorted(binding.target_id for binding in after_replace) == [8, 9]
    assert len(after_replace) == 2
    assert [binding.target_id for binding in after_remove] == [8]
