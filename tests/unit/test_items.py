"""Unit tests for the read-only on-sale item sync pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from xianyu_agent.config import reset_settings_cache
from xianyu_agent.db import Item, database as db_mod
from xianyu_agent.domain import accounts as domain_accounts, items as domain_items
from xianyu_agent.protocol.items_client import (
    ITEM_LIST_URL,
    ItemSyncError,
    RemoteItem,
    XianyuItemsClient,
)
from xianyu_agent.protocol.signer import MtopHeaders


class FakeSigner:
    async def load_cookie_value(self, _account_id: str) -> str:
        return "unb=123; _m_h5_tk=seed_abc"

    async def load_user_id(self, _account_id: str) -> str:
        return "123"

    async def make_headers(self, _account_id: str, *, data: str) -> MtopHeaders:
        assert '"groupName":"在售"' in data
        return MtopHeaders(app_key="34839810", timestamp_ms=123, sign="signed", token="seed_abc")


@pytest.fixture
async def clean_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "items.db"
    monkeypatch.setenv("XIANYU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XIANYU_DB_PATH", str(db))
    reset_settings_cache()
    db_mod.reset_engine()
    await db_mod.init_db()
    await domain_accounts.create_account("acc-items", enabled=True)
    yield
    await db_mod.async_engine.dispose()
    reset_settings_cache()


def _response(*cards: dict) -> dict:
    return {"ret": ["SUCCESS::调用成功"], "data": {"cardList": list(cards)}}


def _card(item_id: str, title: str, price: str = "9.9") -> dict:
    return {
        "cardData": {
            "id": item_id,
            "title": title,
            "priceInfo": {"price": price},
            "itemStatus": 1,
            "detailUrl": f"https://www.goofish.com/item?id={item_id}",
            "picInfo": {"url": f"https://img.example/{item_id}.jpg"},
            "categoryId": "123",
            "auctionType": "a",
        }
    }


@pytest.mark.asyncio
async def test_fetch_on_sale_page_normalizes_response() -> None:
    router = respx.mock(assert_all_called=True)
    route = router.post(ITEM_LIST_URL).mock(
        return_value=httpx.Response(200, json=_response(_card("I-1", "正式商品")))
    )
    with router:
        page = await XianyuItemsClient(FakeSigner()).fetch_on_sale_page("acc-items")
    assert route is not None
    assert page.raw_card_count == 1
    assert page.items[0].item_id == "I-1"
    assert page.items[0].title == "正式商品"
    assert page.items[0].price == "9.9"
    assert page.items[0].main_image_url == "https://img.example/I-1.jpg"


@pytest.mark.asyncio
async def test_fetch_all_follows_pages_until_short_page() -> None:
    router = respx.mock(assert_all_called=True)
    route = router.post(ITEM_LIST_URL).mock(
        side_effect=[
            httpx.Response(200, json=_response(_card("I-1", "商品一"), _card("I-2", "商品二"))),
            httpx.Response(200, json=_response(_card("I-3", "商品三"))),
        ]
    )
    with router:
        snapshot = await XianyuItemsClient(FakeSigner()).fetch_all_on_sale(
            "acc-items", page_size=2
        )
    assert route is not None
    assert snapshot.pages == 2
    assert [item.item_id for item in snapshot.items] == ["I-1", "I-2", "I-3"]


@pytest.mark.asyncio
async def test_fetch_raises_safe_login_error() -> None:
    router = respx.mock(assert_all_called=True)
    router.post(ITEM_LIST_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_SESSION_EXPIRED::登录失效"]})
    )
    with router, pytest.raises(ItemSyncError, match="重新扫码登录"):
        await XianyuItemsClient(FakeSigner()).fetch_on_sale_page("acc-items")


@pytest.mark.asyncio
async def test_apply_complete_snapshot_marks_missing_item_off_sale(clean_db) -> None:
    first = [
        RemoteItem("I-1", "商品一", "9.9", "1", None, None, None, None, {"id": "I-1"}),
        RemoteItem("I-2", "商品二", "19.9", "1", None, None, None, None, {"id": "I-2"}),
    ]
    at = datetime(2026, 8, 12, tzinfo=UTC)
    result = await domain_items.apply_on_sale_snapshot("acc-items", first, synced_at=at)
    assert (result.total, result.created, result.updated, result.marked_off_sale) == (2, 2, 0, 0)

    result2 = await domain_items.apply_on_sale_snapshot(
        "acc-items",
        [RemoteItem("I-2", "商品二改价", "18.8", "1", None, None, None, None, {"id": "I-2"})],
        synced_at=at,
    )
    assert (result2.total, result2.created, result2.updated, result2.marked_off_sale) == (1, 0, 1, 1)
    current = await domain_items.list_items("acc-items")
    assert [item.item_id for item in current] == ["I-2"]
    all_items = await domain_items.list_items("acc-items", on_sale_only=False)
    assert {item.item_id: item.is_on_sale for item in all_items} == {"I-1": False, "I-2": True}
    assert next(item for item in all_items if item.item_id == "I-2").price == "18.8"


@pytest.mark.asyncio
async def test_failed_remote_fetch_does_not_change_existing_snapshot(clean_db) -> None:
    await domain_items.apply_on_sale_snapshot(
        "acc-items",
        [RemoteItem("I-1", "商品一", "9.9", "1", None, None, None, None, {"id": "I-1"})],
    )
    router = respx.mock(assert_all_called=True)
    router.post(ITEM_LIST_URL).mock(
        return_value=httpx.Response(200, json={"ret": ["FAIL_SYS_BUSY::稍后重试"]})
    )
    with router, pytest.raises(ItemSyncError):
        await XianyuItemsClient(FakeSigner()).fetch_all_on_sale("acc-items")
    rows = await domain_items.list_items("acc-items")
    assert len(rows) == 1
    assert rows[0].item_id == "I-1"


@pytest.mark.asyncio
async def test_item_model_is_queryable(clean_db) -> None:
    await domain_items.apply_on_sale_snapshot(
        "acc-items",
        [RemoteItem("I-1", "商品一", "9.9", "1", None, None, None, None, {"id": "I-1"})],
    )
    rows = await domain_items.list_items("acc-items")
    got = await domain_items.get_item(rows[0].id)
    assert got is not None
    assert got.item_id == "I-1"
    assert await domain_items.get_item(99999) is None
    assert isinstance(got, Item)
