"""Regression tests for the order domain responsibility migration."""

from xianyu_agent.cli.commands import item as item_cli
from xianyu_agent.cli.commands import order as order_cli
from xianyu_agent.domain import orders as legacy_orders
from xianyu_agent.domain.order import orders
from xianyu_agent.runtime import account_worker
from xianyu_agent.services import delivery_service


def test_legacy_orders_module_aliases_canonical_order_module() -> None:
    assert legacy_orders is orders
    assert legacy_orders.OrderSyncResult is orders.OrderSyncResult
    assert legacy_orders.ItemSalesSummary is orders.ItemSalesSummary
    assert legacy_orders.upsert_from_event is orders.upsert_from_event
    assert legacy_orders.apply_sold_orders_snapshot is orders.apply_sold_orders_snapshot
    assert legacy_orders.list_for_account is orders.list_for_account
    assert legacy_orders.get_by_id is orders.get_by_id
    assert legacy_orders.sales_by_item is orders.sales_by_item


def test_order_callers_use_canonical_order_module() -> None:
    assert order_cli.domain_orders is orders
    assert item_cli.domain_orders is orders
    assert delivery_service.domain_orders is orders
    assert account_worker.domain_orders is orders
