"""Unit tests for read-only seller-order synchronization and sales aggregation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from sqlalchemy import select

from xianyu_agent.application.analytics import AnalyticsService
from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Order, database as db_mod, get_async_session
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


async def test_sold_order_times_are_parsed_persisted_and_missing_values_keep_known_times(clean_db):
    item = _order("O-times", "I-times", status="交易成功")
    item["commonData"].update(
        {
            "paySuccessTime": "2026-08-21 12:31:00",
            "consignTime": "2026-08-22 00:01:00",
        }
    )
    with respx.mock:
        respx.post(SOLD_ORDERS_URL).mock(return_value=httpx.Response(200, json=_response(item)))
        page = await XianyuOrdersClient(FakeSigner()).fetch_sold_page("acc-orders")
    remote = page.orders[0]
    assert remote.paid_at == datetime(2026, 8, 21, 4, 31, tzinfo=UTC)
    assert remote.delivered_at == datetime(2026, 8, 21, 16, 1, tzinfo=UTC)
    assert remote.raw_payload["commonData"]["paySuccessTime"] == "2026-08-21 12:31:00"
    await domain_orders.apply_sold_orders_snapshot("acc-orders", [remote])
    row = (await domain_orders.list_for_account("acc-orders"))[0]
    assert row.paid_at == remote.paid_at.replace(tzinfo=None)
    assert row.delivered_at == remote.delivered_at.replace(tzinfo=None)
    report = await AnalyticsService().report(
        account_id="acc-orders",
        now=datetime(2026, 8, 23, tzinfo=UTC),
        start_date=date(2026, 8, 21),
        end_date=date(2026, 8, 22),
    )
    assert report.windows[0].paid_orders == 1
    assert report.windows[0].delivered_orders == 1
    assert report.windows[0].amount == 9.9
    assert [day.amount for day in report.revenue_daily] == [9.9, 0]
    assert report.orders_missing_paid_at == report.orders_missing_delivered_at == 0
    await domain_orders.apply_sold_orders_snapshot(
        "acc-orders", [replace(remote, paid_at=None, delivered_at=None)]
    )
    row = (await domain_orders.list_for_account("acc-orders"))[0]
    assert row.paid_at == remote.paid_at.replace(tzinfo=None)
    assert row.delivered_at == remote.delivered_at.replace(tzinfo=None)
    updated = replace(remote, paid_at=datetime(2026, 8, 21, 5, tzinfo=UTC), status="paid")
    await domain_orders.apply_sold_orders_snapshot("acc-orders", [updated])
    row = (await domain_orders.list_for_account("acc-orders"))[0]
    assert row.paid_at == updated.paid_at.replace(tzinfo=None)
    assert row.delivered_at is None


@pytest.mark.parametrize("value", [None, "", "invalid", 123, {"phone": "secret"}])
async def test_missing_invalid_order_times_are_not_inferred_or_retained(value):
    item = _order("O-invalid-time", "I-times", status="已发货")
    item["commonData"].update({"paySuccessTime": value, "consignTime": value})
    with respx.mock:
        respx.post(SOLD_ORDERS_URL).mock(return_value=httpx.Response(200, json=_response(item)))
        page = await XianyuOrdersClient(FakeSigner()).fetch_sold_page("acc-orders")
    assert page.orders[0].paid_at is None
    assert page.orders[0].delivered_at is None
    assert "paySuccessTime" not in page.orders[0].raw_payload["commonData"]
    assert "consignTime" not in page.orders[0].raw_payload["commonData"]


async def test_exact_sold_order_uses_shared_timestamp_parser_and_redaction():
    item = _order("O-exact", "I-exact", status="已发货")
    item["commonData"].update(
        {"paySuccessTime": "2026-08-21 12:31:00", "consignTime": "2026-08-22 00:01:00"}
    )
    with respx.mock as router:
        route = router.post(SOLD_ORDERS_URL).mock(
            return_value=httpx.Response(
                200, json=_response(_order("unmatched", "other"), item, total="0")
            )
        )
        remote = await XianyuOrdersClient(FakeSigner()).fetch_sold_order("acc-orders", "O-exact")
    assert remote.order_id == "O-exact"
    assert remote.paid_at == datetime(2026, 8, 21, 4, 31, tzinfo=UTC)
    assert remote.delivered_at == datetime(2026, 8, 21, 16, 1, tzinfo=UTC)
    assert "phone" not in str(remote.raw_payload)
    payload = json.loads(parse_qs(route.calls[0].request.content.decode())["data"][0])
    assert payload["orderIds"] == "O-exact"


@pytest.mark.asyncio
async def test_sold_title_is_parsed_persisted_and_updated_without_losing_known_title(
    clean_db,
) -> None:
    item = _order("O-title", "I-title")
    item["itemVO"] = {"title": "中文商品 & PDF / 套餐", "phone": "不应保留的商品附加字段"}
    with respx.mock:
        respx.post(SOLD_ORDERS_URL).mock(return_value=httpx.Response(200, json=_response(item)))
        page = await XianyuOrdersClient(FakeSigner()).fetch_sold_page("acc-orders")
    assert page.orders[0].item_title == "中文商品 & PDF / 套餐"
    assert page.orders[0].raw_payload["itemVO"] == {"title": "中文商品 & PDF / 套餐"}
    await domain_orders.apply_sold_orders_snapshot("acc-orders", page.orders)
    row = (await domain_orders.list_for_account("acc-orders"))[0]
    assert row.item_title == "中文商品 & PDF / 套餐"

    item["itemVO"]["title"] = "订单中的新标题"
    with respx.mock:
        route = respx.post(SOLD_ORDERS_URL).mock(
            side_effect=[
                httpx.Response(200, json=_response(item)),
                httpx.Response(200, json=_response(_order("O-title", "I-title"))),
            ]
        )
        page = await XianyuOrdersClient(FakeSigner()).fetch_sold_page("acc-orders")
        await domain_orders.apply_sold_orders_snapshot("acc-orders", page.orders)
        row = (await domain_orders.list_for_account("acc-orders"))[0]
        assert row.item_title == "订单中的新标题"
        page = await XianyuOrdersClient(FakeSigner()).fetch_sold_page("acc-orders")
        await domain_orders.apply_sold_orders_snapshot("acc-orders", page.orders)
    assert route.call_count == 2
    row = (await domain_orders.list_for_account("acc-orders"))[0]
    assert row.item_title == "订单中的新标题"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item_vo",
    [
        None,
        [],
        "invalid",
        {},
        {"title": None},
        {"title": ""},
        {"title": "  "},
        {"title": {"phone": "secret"}},
    ],
)
async def test_missing_or_invalid_sold_title_is_not_stringified_or_retained(item_vo) -> None:
    item = _order("O-1", "I-1")
    item["itemVO"] = item_vo
    with respx.mock:
        respx.post(SOLD_ORDERS_URL).mock(return_value=httpx.Response(200, json=_response(item)))
        page = await XianyuOrdersClient(FakeSigner()).fetch_sold_page("acc-orders")
    assert page.orders[0].item_title is None
    assert "itemVO" not in page.orders[0].raw_payload
    assert "secret" not in str(page.orders[0].raw_payload)


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


@pytest.mark.asyncio
async def test_paid_snapshot_clears_stale_delivery_timestamp(clean_db) -> None:
    await domain_orders.apply_sold_orders_snapshot(
        "acc-orders", [_remote("O-1", "I-1", quantity=1, status="paid")]
    )
    async with get_async_session() as session:
        row = (await session.execute(select(Order).where(Order.order_id == "O-1"))).scalar_one()
        row.delivered_at = datetime.now(UTC)
        await session.commit()

    await domain_orders.apply_sold_orders_snapshot(
        "acc-orders", [_remote("O-1", "I-1", quantity=1, status="paid")]
    )
    row = (await domain_orders.list_for_account("acc-orders", limit=10))[0]
    assert row.status == "paid"
    assert row.delivered_at is None


@pytest.mark.asyncio
async def test_exact_order_info_preserves_masking_and_zero_values_without_raw_payload() -> None:

    item = _order("O-1", "I-1")
    item["buyerInfoVO"].update(
        {
            "phone": "138****5678",
            "name": "王*",
            "isPrivacy": "false",
            "userIcon": "https://example.com/avatar.jpg",
        }
    )
    item["priceVO"].update(
        {"postFee": "0.00", "discountFee": 0, "confirmFee": "9.90", "refundFee": "0.00"}
    )
    item["commonData"].update({"paySuccessTime": "2026-08-21 12:31:00", "inRefund": "false"})
    item["cookie"] = "do-not-expose"
    with respx.mock as router:
        route = router.post(SOLD_ORDERS_URL).mock(
            return_value=httpx.Response(
                200, json=_response(_order("O-other", "other"), item, total="0")
            )
        )
        data = await XianyuOrdersClient(FakeSigner()).fetch_order_info("acc-orders", "O-1")

    payload = json.loads(parse_qs(route.calls[0].request.content.decode())["data"][0])
    assert payload["orderIds"] == "O-1"
    assert data["order_id"] == "O-1"
    assert data["receiver_phone"] == "138****5678"
    assert data["is_privacy"] is False
    assert data["in_refund"] is False
    assert data["discount_fee"] == 0
    assert data["post_fee"] == "0.00"
    assert data["paid_at"] == "2026-08-21 12:31:00"
    assert "raw_payload" not in data
    assert "do-not-expose" not in str(data)


@pytest.mark.asyncio
async def test_exact_order_info_rejects_unmatched_result() -> None:
    with respx.mock as router:
        router.post(SOLD_ORDERS_URL).mock(
            return_value=httpx.Response(200, json=_response(_order("O-other", "I-1")))
        )
        with pytest.raises(OrderSyncError, match="平台未返回该订单"):
            await XianyuOrdersClient(FakeSigner()).fetch_order_info("acc-orders", "O-1")
