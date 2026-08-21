"""Unit tests for read-only seller-order synchronization and sales aggregation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, orders as domain_orders
from xianyu_agent.protocol.orders_client import (
    SOLD_ORDERS_URL,
    OrderSyncError,
    RemoteSoldOrder,
    XianyuOrdersClient,
)


class FakeSigner:
    async def load_cookie_value(self, _account_id: str) -> str:
        return "unb=123; _m_h5_tk=seed_abc"


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "orders-sync.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-orders", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _order(
    order_id: str,
    item_id: str,
    *,
    status: str = "待发货",
    quantity: str = "1",
    amount: str = "9.90",
) -> dict:
    return {
        "commonData": {
            "orderId": order_id,
            "itemId": item_id,
            "orderStatus": status,
            "createTime": "2026-08-21 12:30:00",
        },
        "buyerInfoVO": {
            "buyerId": f"buyer-{order_id}",
            "userNick": "测试买家",
            "phone": "13800000000",
            "address": "不应落库的收货地址",
        },
        "priceVO": {"totalPrice": amount, "buyNum": quantity},
    }


def _response(*items: dict, total: str = "1", next_page: str = "false") -> dict:
    return {
        "ret": ["SUCCESS::调用成功"],
        "data": {"module": {"items": list(items), "totalCount": total, "nextPage": next_page}},
    }


@pytest.mark.asyncio
async def test_fetch_sold_orders_normalizes_and_redacts_response() -> None:
    router = respx.mock(assert_all_called=True)
    route = router.post(SOLD_ORDERS_URL).mock(
        return_value=httpx.Response(200, json=_response(_order("O-1", "I-1", quantity="2")))
    )
    with router:
        page = await XianyuOrdersClient(FakeSigner()).fetch_sold_page("acc-orders")
    assert route is not None
    assert page.total_count == 1
    assert page.has_next_page is False
    assert page.orders[0].status == "paid"
    assert page.orders[0].quantity == 2
    assert page.orders[0].placed_at == datetime(2026, 8, 21, 4, 30, tzinfo=UTC)
    assert page.orders[0].raw_payload["buyerInfoVO"] == {
        "buyerId": "buyer-O-1",
        "userNick": "测试买家",
    }
    assert "phone" not in str(page.orders[0].raw_payload)
    assert "收货地址" not in str(page.orders[0].raw_payload)


@pytest.mark.asyncio
async def test_fetch_all_sold_follows_next_page() -> None:
    router = respx.mock(assert_all_called=True)
    route = router.post(SOLD_ORDERS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json=_response(_order("O-1", "I-1"), total="2", next_page="true"),
            ),
            httpx.Response(200, json=_response(_order("O-2", "I-2"), total="2")),
        ]
    )
    with router:
        snapshot = await XianyuOrdersClient(FakeSigner()).fetch_all_sold(
            "acc-orders",
            page_size=1,
        )
    assert route is not None
    assert snapshot.pages == 2
    assert snapshot.reported_total == 2
    assert [order.order_id for order in snapshot.orders] == ["O-1", "O-2"]


@pytest.mark.asyncio
async def test_fetch_sold_orders_raises_safe_login_error() -> None:
    router = respx.mock(assert_all_called=True)
    router.post(SOLD_ORDERS_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_SESSION_EXPIRED::登录失效"]})
    )
    with router, pytest.raises(OrderSyncError, match="重新扫码登录"):
        await XianyuOrdersClient(FakeSigner()).fetch_sold_page("acc-orders")


def _remote(
    order_id: str,
    item_id: str,
    *,
    quantity: int,
    status: str,
) -> RemoteSoldOrder:
    return RemoteSoldOrder(
        order_id=order_id,
        item_id=item_id,
        buyer_id="buyer",
        buyer_name="买家",
        amount=9.9,
        quantity=quantity,
        status=status,
        placed_at=datetime(2026, 8, 21, tzinfo=UTC),
        raw_payload={"commonData": {"orderId": order_id, "itemId": item_id}},
    )


@pytest.mark.asyncio
async def test_snapshot_persists_orders_and_sales_excludes_refunds(clean_db) -> None:
    first = [
        _remote("O-1", "I-1", quantity=2, status="paid"),
        _remote("O-2", "I-1", quantity=3, status="refunded"),
        _remote("O-3", "I-2", quantity=1, status="completed"),
    ]
    result = await domain_orders.apply_sold_orders_snapshot("acc-orders", first)
    assert (result.total, result.created, result.updated) == (3, 3, 0)
    sales = await domain_orders.sales_by_item("acc-orders")
    assert sales["I-1"].sold_quantity == 2
    assert sales["I-1"].sold_order_count == 1
    assert sales["I-2"].sold_quantity == 1

    second = [_remote("O-1", "I-1", quantity=4, status="delivered")]
    result = await domain_orders.apply_sold_orders_snapshot("acc-orders", second)
    assert (result.total, result.created, result.updated) == (1, 0, 1)
    rows = await domain_orders.list_for_account("acc-orders", limit=10)
    assert {row.order_id for row in rows} == {"O-1", "O-2", "O-3"}
    sales = await domain_orders.sales_by_item("acc-orders")
    assert sales["I-1"].sold_quantity == 4
