"""Migration contracts for canonical read-only MTOP endpoint clients."""

from __future__ import annotations

import httpx
import pytest
import respx

from xianyu_agent.protocol import items_client as legacy_items
from xianyu_agent.protocol import orders_client as legacy_orders
from xianyu_agent.protocol.mtop import endpoints, signer as canonical_signer
from xianyu_agent.protocol.mtop.endpoints import items as canonical_items
from xianyu_agent.protocol.mtop.endpoints import orders as canonical_orders


def test_legacy_items_surface_is_canonical_identity() -> None:
    assert legacy_items.XianyuItemsClient is canonical_items.XianyuItemsClient
    assert legacy_items.RemoteItem is canonical_items.RemoteItem
    assert legacy_items.ItemPage is canonical_items.ItemPage
    assert legacy_items.ItemSnapshot is canonical_items.ItemSnapshot
    assert legacy_items.ItemSyncError is canonical_items.ItemSyncError
    assert legacy_items.ITEM_LIST_API == canonical_items.ITEM_LIST_API
    assert legacy_items.ITEM_LIST_URL == canonical_items.ITEM_LIST_URL
    assert endpoints.XianyuItemsClient is canonical_items.XianyuItemsClient


def test_legacy_orders_surface_is_canonical_identity() -> None:
    assert legacy_orders.XianyuOrdersClient is canonical_orders.XianyuOrdersClient
    assert legacy_orders.RemoteSoldOrder is canonical_orders.RemoteSoldOrder
    assert legacy_orders.SoldOrderPage is canonical_orders.SoldOrderPage
    assert legacy_orders.SoldOrderSnapshot is canonical_orders.SoldOrderSnapshot
    assert legacy_orders.OrderSyncError is canonical_orders.OrderSyncError
    assert legacy_orders.SOLD_ORDERS_API == canonical_orders.SOLD_ORDERS_API
    assert legacy_orders.SOLD_ORDERS_URL == canonical_orders.SOLD_ORDERS_URL
    assert endpoints.XianyuOrdersClient is canonical_orders.XianyuOrdersClient


def test_canonical_endpoints_own_signer_dependency_and_remain_read_only() -> None:
    assert canonical_items.CookieSigner is canonical_signer.CookieSigner
    assert canonical_items.compute_sign is canonical_signer.compute_sign
    assert canonical_items.extract_mtop_token is canonical_signer.extract_mtop_token
    assert canonical_items.make_headers is canonical_signer.make_headers
    assert canonical_orders.CookieSigner is canonical_signer.CookieSigner
    assert canonical_orders.compute_sign is canonical_signer.compute_sign
    assert canonical_orders.extract_mtop_token is canonical_signer.extract_mtop_token
    assert canonical_orders.make_headers is canonical_signer.make_headers

    item_public = {
        name
        for name, value in vars(canonical_items.XianyuItemsClient).items()
        if not name.startswith("_") and callable(value)
    }
    order_public = {
        name
        for name, value in vars(canonical_orders.XianyuOrdersClient).items()
        if not name.startswith("_") and callable(value)
    }
    assert item_public == {"fetch_all_on_sale", "fetch_on_sale_page"}
    assert order_public == {"fetch_all_sold", "fetch_sold_page"}


class _ItemsBootstrapSigner:
    async def load_cookie_value(self, _account_id: str) -> str:
        return "unb=123; cookie2=existing"

    async def load_user_id(self, _account_id: str) -> str:
        return "123"


class _OrdersBootstrapSigner:
    async def load_cookie_value(self, _account_id: str) -> str:
        return "unb=123; cookie2=existing"


@pytest.mark.asyncio
async def test_items_bootstrap_set_cookie_merge_survives_migration() -> None:
    router = respx.mock(assert_all_called=True)
    route = router.post(canonical_items.ITEM_LIST_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"ret": ["FAIL_SYS_TOKEN_EXPIRED::token"]},
                headers={"Set-Cookie": "_m_h5_tk=seed_abc; Path=/"},
            ),
            httpx.Response(200, json={"ret": ["SUCCESS::调用成功"], "data": {"cardList": []}}),
        ]
    )
    with router:
        page = await canonical_items.XianyuItemsClient(
            _ItemsBootstrapSigner()
        ).fetch_on_sale_page("acc-items")

    assert route.call_count == 2
    assert page.items == []
    assert page.raw_card_count == 0


@pytest.mark.asyncio
async def test_orders_bootstrap_set_cookie_merge_survives_migration() -> None:
    router = respx.mock(assert_all_called=True)
    route = router.post(canonical_orders.SOLD_ORDERS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"ret": ["FAIL_SYS_TOKEN_EXPIRED::token"]},
                headers={"Set-Cookie": "_m_h5_tk=seed_abc; Path=/"},
            ),
            httpx.Response(
                200,
                json={
                    "ret": ["SUCCESS::调用成功"],
                    "data": {
                        "module": {"items": [], "totalCount": "0", "nextPage": "false"}
                    },
                },
            ),
        ]
    )
    with router:
        page = await canonical_orders.XianyuOrdersClient(
            _OrdersBootstrapSigner(), page_delay_s=0
        ).fetch_sold_page("acc-orders")

    assert route.call_count == 2
    assert page.orders == []
    assert page.total_count == 0
    assert page.has_next_page is False
