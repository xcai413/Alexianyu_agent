"""Regression tests for the item domain responsibility migration."""

from xianyu_agent.application import item as item_application
from xianyu_agent.application.item import query_service
from xianyu_agent.cli.commands import item as item_cli
from xianyu_agent.domain import items as legacy_items
from xianyu_agent.domain.item import items


def test_legacy_items_module_aliases_canonical_item_module() -> None:
    assert legacy_items is items
    assert legacy_items.ItemSyncResult is items.ItemSyncResult
    assert legacy_items.apply_on_sale_snapshot is items.apply_on_sale_snapshot
    assert legacy_items.list_items is items.list_items
    assert legacy_items.get_item is items.get_item


def test_item_cli_uses_the_application_service_not_the_domain_module() -> None:
    """The item CLI is an adapter: orchestration lives in application/item."""
    assert item_cli.ItemSyncService is item_application.ItemSyncService
    assert item_cli.ItemQueryService is item_application.ItemQueryService
    assert query_service.domain_items is items
