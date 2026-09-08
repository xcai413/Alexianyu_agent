"""Regression tests for the item domain responsibility migration."""

from xianyu_agent.cli.commands import item as item_cli
from xianyu_agent.domain import items as legacy_items
from xianyu_agent.domain.item import items


def test_legacy_items_module_aliases_canonical_item_module() -> None:
    assert legacy_items is items
    assert legacy_items.ItemSyncResult is items.ItemSyncResult
    assert legacy_items.apply_on_sale_snapshot is items.apply_on_sale_snapshot
    assert legacy_items.list_items is items.list_items
    assert legacy_items.get_item is items.get_item


def test_item_cli_uses_canonical_item_module() -> None:
    assert item_cli.domain_items is items
