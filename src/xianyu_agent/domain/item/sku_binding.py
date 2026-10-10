"""规格组合到库存来源的映射(M10 的规格→库存/卡密部分)。

把"买家选了哪个规格"翻译成"从哪个库存来源取几份"。每条映射以一个精确规格组合为键,
不做部分匹配:买的是蓝色 M 就必须命中蓝色 M 的映射,命中不了就是没配置,而不是回退到
别的规格成交。

目标来源用 target_kind 显式区分,不靠 id 猜类型:card 是旧卡密组,pool 是 Phase 6 的库存池。
两种 kind 共用同一张绑定表,因此旧配置不需要迁移。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xianyu_agent.db import Account, Item, ItemSpecBinding, get_async_session

from .sku import SpecValue, normalize_specs, spec_key, specs_from_stored, specs_to_stored

TARGET_KINDS: tuple[str, ...] = ("card", "pool")
CARD_TARGET = "card"
POOL_TARGET = "pool"


@dataclass(frozen=True)
class SpecBinding:
    """One spec combination and the inventory source that serves it."""

    specs: tuple[SpecValue, ...]
    target_id: int
    target_kind: str = CARD_TARGET
    quantity: int = 1
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.specs:
            msg = "绑定必须至少包含一个规格"
            raise ValueError(msg)
        if len({spec.name for spec in self.specs}) != len(self.specs):
            msg = "同一绑定内的规格名不能重复"
            raise ValueError(msg)
        if self.target_kind not in TARGET_KINDS:
            msg = f"未知库存来源类型: {self.target_kind}"
            raise ValueError(msg)
        if self.target_id <= 0:
            msg = "库存来源 id 必须为正整数"
            raise ValueError(msg)
        if self.quantity < 1:
            msg = "每次发货数量至少为 1"
            raise ValueError(msg)

    @property
    def key(self) -> str:
        return binding_key(self.specs)

    def describe(self) -> str:
        return " ".join(f"{spec.name}:{spec.value}" for spec in self.specs)


def binding_key(specs: Sequence[SpecValue]) -> str:
    """Stable string identity of one spec combination, used as the unique key.

    Built from canonical JSON rather than a hand-made separator so a spec name or
    value containing that separator cannot collide with another combination.
    """
    return json.dumps(spec_key(specs), ensure_ascii=False, separators=(",", ":"))


def validate_bindings(bindings: Sequence[SpecBinding]) -> tuple[str, ...]:
    """Return problems that make the binding set unusable; empty means fine."""
    problems: list[str] = []
    seen: set[str] = set()
    for binding in bindings:
        if binding.key in seen:
            problems.append(f"规格组合重复配置: {binding.describe()}")
        seen.add(binding.key)
    return tuple(problems)


def build_binding_index(bindings: Sequence[SpecBinding]) -> dict[str, SpecBinding]:
    """Index bindings by spec key; raises when the set is contradictory."""
    problems = validate_bindings(bindings)
    if problems:
        msg = "; ".join(problems)
        raise ValueError(msg)
    return {binding.key: binding for binding in bindings}


@dataclass(frozen=True)
class BindingResolution:
    """Which inventory source serves one buyer selection, and why."""

    selection_key: str
    binding: SpecBinding | None = None
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.binding is not None and self.binding.enabled


def resolve_binding(
    bindings: Sequence[SpecBinding],
    selection: Mapping[str, str] | Sequence[SpecValue],
) -> BindingResolution:
    """Resolve one buyer selection against the configured bindings."""
    wanted = selection if isinstance(selection, Mapping) else _pairs(selection)
    selection_key = binding_key(normalize_specs(wanted))
    binding = build_binding_index(bindings).get(selection_key)
    if binding is None:
        return BindingResolution(selection_key, None, "该规格组合未配置库存来源")
    if not binding.enabled:
        return BindingResolution(selection_key, binding, "该规格组合的库存来源已停用")
    return BindingResolution(selection_key, binding, "命中规格库存来源")


def _pairs(specs: Sequence[SpecValue]) -> list[tuple[str, str]]:
    return [(spec.name, spec.value) for spec in specs]


async def replace_item_bindings(
    account_id: str, item_id: str, bindings: Sequence[SpecBinding]
) -> int:
    """Replace all bindings of one mirrored item; returns rows written.

    Replacement is all-or-nothing per item: a partial update would leave the
    operator guessing which spec still points where.
    """
    problems = validate_bindings(bindings)
    if problems:
        msg = "; ".join(problems)
        raise ValueError(msg)
    async with get_async_session() as session:
        item = await _find_item(session, account_id=account_id, item_id=item_id)
        if item is None:
            msg = f"商品 {item_id} 不存在"
            raise ValueError(msg)
        existing = list(
            (
                await session.execute(
                    select(ItemSpecBinding).where(ItemSpecBinding.item_pk == item.id)
                )
            )
            .scalars()
            .all()
        )
        by_key = {row.spec_key: row for row in existing}
        seen: set[str] = set()
        for binding in bindings:
            seen.add(binding.key)
            row = by_key.get(binding.key)
            if row is None:
                row = ItemSpecBinding(item_pk=item.id, spec_key=binding.key)
                session.add(row)
            row.specs = specs_to_stored(binding.specs)
            row.target_kind = binding.target_kind
            row.target_id = binding.target_id
            row.quantity = binding.quantity
            row.enabled = binding.enabled
        for spec_key_value, stale in by_key.items():
            if spec_key_value not in seen:
                await session.delete(stale)
        await session.commit()
        return len(seen)


async def list_item_bindings(account_id: str, item_id: str) -> tuple[SpecBinding, ...]:
    """Read the configured bindings of one mirrored item."""
    async with get_async_session() as session:
        item = await _find_item(session, account_id=account_id, item_id=item_id)
        if item is None:
            return ()
        rows = list(
            (
                await session.execute(
                    select(ItemSpecBinding)
                    .where(ItemSpecBinding.item_pk == item.id)
                    .order_by(ItemSpecBinding.id.asc())
                )
            )
            .scalars()
            .all()
        )
    return tuple(_binding_from_row(row) for row in rows)


async def upsert_item_binding(
    account_id: str, item_id: str, binding: SpecBinding
) -> tuple[SpecBinding, ...]:
    """Add or replace one binding, keeping the item's other bindings.

    Adapters configure one spec combination at a time, so replacing the whole set
    from a single-row command would silently drop the rest.
    """
    existing = await list_item_bindings(account_id, item_id)
    merged = [current for current in existing if current.key != binding.key]
    merged.append(binding)
    await replace_item_bindings(account_id, item_id, merged)
    return await list_item_bindings(account_id, item_id)


async def remove_item_binding(
    account_id: str, item_id: str, selection: Mapping[str, str] | Sequence[SpecValue]
) -> tuple[SpecBinding, ...]:
    """Remove one binding by spec combination; returns what is left."""
    wanted = selection if isinstance(selection, Mapping) else _pairs(selection)
    key = binding_key(normalize_specs(wanted))
    existing = await list_item_bindings(account_id, item_id)
    remaining = [current for current in existing if current.key != key]
    await replace_item_bindings(account_id, item_id, remaining)
    return await list_item_bindings(account_id, item_id)


async def resolve_item_binding(
    account_id: str,
    item_id: str,
    selection: Mapping[str, str] | Sequence[SpecValue],
) -> BindingResolution | None:
    """Resolve one selection for one item; None when the item is unknown."""
    bindings = await list_item_bindings(account_id, item_id)
    if not bindings:
        item_exists = await _item_exists(account_id=account_id, item_id=item_id)
        if not item_exists:
            return None
    return resolve_binding(bindings, selection)


async def _find_item(session: AsyncSession, *, account_id: str, item_id: str) -> Item | None:
    account = (
        await session.execute(select(Account).where(Account.account_id == account_id).limit(1))
    ).scalar_one_or_none()
    if account is None:
        return None
    return (
        await session.execute(
            select(Item).where(Item.account_id == account.id, Item.item_id == item_id).limit(1)
        )
    ).scalar_one_or_none()


async def _item_exists(*, account_id: str, item_id: str) -> bool:
    async with get_async_session() as session:
        return await _find_item(session, account_id=account_id, item_id=item_id) is not None


def _binding_from_row(row: ItemSpecBinding) -> SpecBinding:
    return SpecBinding(
        specs=specs_from_stored(row.specs),
        target_id=int(row.target_id),
        target_kind=str(row.target_kind),
        quantity=int(row.quantity),
        enabled=bool(row.enabled),
    )
