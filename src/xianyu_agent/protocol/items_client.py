"""只读拉取账号在售商品的 mtop 客户端。

该客户端只调用商品列表接口; 不会发起上架、下架、编辑、删除或购买请求。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from xianyu_agent.protocol.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_mtop_token,
    make_headers,
)

ITEM_LIST_API = "mtop.idle.web.xyh.item.list"
ITEM_LIST_URL = f"https://h5api.m.goofish.com/h5/{ITEM_LIST_API}/1.0/"
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class ItemSyncError(RuntimeError):
    """在售商品同步失败。"""


@dataclass(frozen=True)
class RemoteItem:
    """从商品列表响应标准化出的最小在售商品信息。"""

    item_id: str
    title: str
    price: str | None
    status: str | None
    detail_url: str | None
    main_image_url: str | None
    category_id: str | None
    auction_type: str | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class ItemPage:
    """单页的标准化商品结果。"""

    items: list[RemoteItem]
    page_number: int
    page_size: int
    raw_card_count: int


@dataclass(frozen=True)
class ItemSnapshot:
    """完整在售商品快照。"""

    items: list[RemoteItem]
    fetched_at: datetime
    pages: int


class XianyuItemsClient:
    """在售商品只读客户端。"""

    def __init__(self, signer: CookieSigner | None = None, *, timeout_s: float = 20.0) -> None:
        self._signer = signer or CookieSigner()
        self._timeout = httpx.Timeout(timeout_s)

    async def fetch_on_sale_page(
        self, account_id: str, *, page_number: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> ItemPage:
        """拉取一页“在售”商品,返回标准化字段且不改变远端状态。"""
        if page_number < 1:
            msg = "page_number 必须 >= 1"
            raise ValueError(msg)
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            msg = f"page_size 必须在 1-{MAX_PAGE_SIZE}"
            raise ValueError(msg)
        cookie, unb = await self._load_item_auth(account_id)
        payload = _make_list_payload(unb, page_number=page_number, page_size=page_size)
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        token = extract_mtop_token(cookie)
        response = (
            await self._request_list(cookie, token, data)
            if token
            else await self._bootstrap_list_session(account_id, cookie, data)
        )
        return _parse_item_page(response, account_id, page_number=page_number, page_size=page_size)

    async def _load_item_auth(self, account_id: str) -> tuple[str, str]:
        """Load Cookie and platform user ID without exposing either value."""
        cookie = await self._signer.load_cookie_value(account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise ItemSyncError(msg)
        unb = await self._signer.load_user_id(account_id)
        if not unb:
            msg = f"账号 {account_id} Cookie 缺少 unb,请重新扫码登录"
            raise ItemSyncError(msg)
        return cookie, unb

    async def _request_list(self, cookie: str, token: str, data: str) -> httpx.Response:
        """Send the only remote item-list request in this module."""
        params = _make_list_params(token, data)
        headers = {**_BROWSER_HEADERS, "Cookie": cookie}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                return await client.post(
                    ITEM_LIST_URL, params=params, data={"data": data}, headers=headers
                )
        except httpx.HTTPError as exc:
            msg = f"商品列表网络请求失败({type(exc).__name__})"
            raise ItemSyncError(msg) from exc

    async def _bootstrap_list_session(
        self, account_id: str, cookie: str, data: str
    ) -> httpx.Response:
        """Run the platform's token-bootstrap handshake through the read-only list API.

        The first request has an empty token seed. A successful bootstrap may set
        ``_m_h5_tk``/``m_h5_tk`` and is then followed by the signed list request.
        Values remain in memory and are not written or logged.
        """
        bootstrap = await self._request_list(cookie, "", data)
        merged_cookie = _merge_set_cookies(cookie, bootstrap)
        token = extract_mtop_token(merged_cookie)
        if token:
            return await self._request_list(merged_cookie, token, data)
        if _response_is_success(bootstrap):
            return bootstrap
        msg = f"账号 {account_id} bootstrap 未返回 mtop token,请重新扫码登录"
        raise ItemSyncError(msg)

    async def fetch_all_on_sale(
        self, account_id: str, *, page_size: int = DEFAULT_PAGE_SIZE, max_pages: int = 100
    ) -> ItemSnapshot:
        """拉取完整在售快照。

        只有完整快照成功返回后,调用方才可以把未出现在快照里的旧商品标记为非在售。
        """
        if max_pages < 1:
            msg = "max_pages 必须 >= 1"
            raise ValueError(msg)
        items: list[RemoteItem] = []
        for page_number in range(1, max_pages + 1):
            page = await self.fetch_on_sale_page(
                account_id, page_number=page_number, page_size=page_size
            )
            items.extend(page.items)
            if page.raw_card_count < page_size:
                return ItemSnapshot(
                    items=items,
                    fetched_at=datetime.now(UTC),
                    pages=page_number,
                )
        msg = f"商品页数达到安全上限 {max_pages},未写入本地镜像"
        raise ItemSyncError(msg)


def _make_list_payload(unb: str, *, page_number: int, page_size: int) -> dict[str, Any]:
    return {
        "needGroupInfo": False,
        "pageNumber": page_number,
        "pageSize": page_size,
        "groupName": "在售",
        "groupId": "58877261",
        "defaultGroup": True,
        "userId": unb,
    }


def _make_list_params(token: str, data: str) -> dict[str, str]:
    if token:
        signed = make_headers(token, data=data)
        timestamp = signed.x_t
        sign = signed.x_sign
    else:
        timestamp = str(int(time.time() * 1000))
        sign = compute_sign("", int(timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": timestamp,
        "sign": sign,
        "v": "1.0",
        "type": "originaljson",
        "dataType": "json",
        "timeout": "20000",
        "api": ITEM_LIST_API,
        "sessionOption": "AutoLoginOnly",
    }


def _parse_item_page(
    response: httpx.Response, account_id: str, *, page_number: int, page_size: int
) -> ItemPage:
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        msg = f"商品列表返回非 JSON(HTTP {response.status_code})"
        raise ItemSyncError(msg) from exc
    ret_text = _response_ret_text(body)
    if response.status_code != 200 or not ret_text.startswith("SUCCESS"):
        error_upper = ret_text.upper()
        if any(marker in error_upper for marker in ("TOKEN", "LOGIN", "SESSION", "EXPIRED")):
            msg = f"账号 {account_id} Cookie 已失效,请重新扫码登录"
        else:
            msg = f"商品列表请求失败: {ret_text or f'HTTP {response.status_code}'}"
        raise ItemSyncError(msg)
    data_body = body.get("data")
    if not isinstance(data_body, dict):
        msg = "商品列表响应缺少 data"
        raise ItemSyncError(msg)
    cards = data_body.get("cardList", [])
    if not isinstance(cards, list):
        msg = "商品列表响应 cardList 格式异常"
        raise ItemSyncError(msg)
    items = [item for card in cards if (item := _parse_card(card)) is not None]
    return ItemPage(
        items=items,
        page_number=page_number,
        page_size=page_size,
        raw_card_count=len(cards),
    )


def _response_is_success(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return False
    return response.status_code == 200 and _response_ret_text(body).startswith("SUCCESS")


def _response_ret_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    ret = body.get("ret")
    return str(ret[0]) if isinstance(ret, list) and ret else ""


def _parse_card(card: Any) -> RemoteItem | None:
    if not isinstance(card, dict):
        return None
    data = card.get("cardData")
    if not isinstance(data, dict):
        return None
    item_id = str(data.get("id") or "")
    title = str(data.get("title") or "")
    if not item_id or not title:
        return None
    price_info = data.get("priceInfo")
    price = str(price_info.get("price")) if isinstance(price_info, dict) and price_info.get("price") is not None else None
    pic_info = data.get("picInfo")
    main_image_url = _first_image_url(pic_info)
    return RemoteItem(
        item_id=item_id,
        title=title,
        price=price,
        status=str(data.get("itemStatus")) if data.get("itemStatus") is not None else None,
        detail_url=str(data.get("detailUrl")) if data.get("detailUrl") else None,
        main_image_url=main_image_url,
        category_id=str(data.get("categoryId")) if data.get("categoryId") is not None else None,
        auction_type=str(data.get("auctionType")) if data.get("auctionType") is not None else None,
        raw_payload=data,
    )


def _first_image_url(pic_info: Any) -> str | None:
    if not isinstance(pic_info, dict):
        return None
    for key in ("url", "mainPicUrl", "picUrl"):
        value = pic_info.get(key)
        if value:
            return str(value)
    pictures = pic_info.get("picList") or pic_info.get("pics")
    if isinstance(pictures, list) and pictures:
        first = pictures[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            for key in ("url", "picUrl"):
                if first.get(key):
                    return str(first[key])
    return None


def _merge_set_cookies(existing_cookie: str, response: httpx.Response) -> str:
    """Merge Set-Cookie names into a Cookie header without exposing values."""
    values: dict[str, str] = {}
    for part in existing_cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            values[name] = value
    for name, value in response.headers.multi_items():
        if name.lower() != "set-cookie":
            continue
        first = value.split(";", 1)[0]
        cookie_name, separator, cookie_value = first.partition("=")
        if separator and cookie_name.strip():
            values[cookie_name.strip()] = cookie_value.strip()
    return "; ".join(f"{name}={value}" for name, value in values.items())
