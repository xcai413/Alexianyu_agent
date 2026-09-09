"""只读同步卖家已售订单的 mtop 客户端。

本模块只读取卖家订单列表并构建本地镜像, 绝不发货、退款、评价或修改闲鱼订单。
接口与字段需要通过 ``order sync`` 的真实只读结果持续校准; 失败时调用方不得改写
现有本地订单。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_mtop_token,
    make_headers,
)

SOLD_ORDERS_API = "mtop.taobao.idle.trade.merchant.sold.get"
SOLD_ORDERS_URL = f"https://h5api.m.goofish.com/h5/{SOLD_ORDERS_API}/1.0/"
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 50
_ORDER_TIME_ZONE = ZoneInfo("Asia/Shanghai")

_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://seller.goofish.com",
    "Referer": "https://seller.goofish.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "idle_site_biz_code": "COMMONPRO",
}

_STATUS_MAP = {
    "待付款": "pending_payment",
    "待发货": "paid",
    "已发货": "delivered",
    "交易成功": "completed",
    "交易关闭": "cancelled",
    "退款中": "refunded",
    "退款成功": "refunded",
    "已退款": "refunded",
    "退款关闭": "cancelled",
}


class OrderSyncError(RuntimeError):
    """卖家订单只读同步失败。"""


@dataclass(frozen=True)
class RemoteSoldOrder:
    """从卖家订单列表响应标准化出的最小订单信息。"""

    order_id: str
    item_id: str | None
    buyer_id: str | None
    buyer_name: str | None
    amount: float
    quantity: int
    status: str
    placed_at: datetime | None
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class SoldOrderPage:
    """单页卖家订单结果。"""

    orders: list[RemoteSoldOrder]
    page_number: int
    page_size: int
    total_count: int
    has_next_page: bool


@dataclass(frozen=True)
class SoldOrderSnapshot:
    """一次完整卖家订单同步快照。"""

    orders: list[RemoteSoldOrder]
    fetched_at: datetime
    pages: int
    reported_total: int


class XianyuOrdersClient:
    """卖家已售订单的只读 mtop 客户端。"""

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        timeout_s: float = 20.0,
        page_delay_s: float = 1.0,
    ) -> None:
        if page_delay_s < 0:
            msg = "page_delay_s 必须 >= 0"
            raise ValueError(msg)
        self._signer = signer or CookieSigner()
        self._timeout = httpx.Timeout(timeout_s)
        self._page_delay_s = page_delay_s

    async def fetch_sold_page(
        self,
        account_id: str,
        *,
        page_number: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> SoldOrderPage:
        """拉取一页卖家订单; 请求只读, 不修改闲鱼端订单。"""
        if page_number < 1:
            msg = "page_number 必须 >= 1"
            raise ValueError(msg)
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            msg = f"page_size 必须在 1-{MAX_PAGE_SIZE}"
            raise ValueError(msg)
        cookie = await self._load_order_auth(account_id)
        data = json.dumps(
            _make_sold_orders_payload(page_number=page_number, page_size=page_size),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        token = extract_mtop_token(cookie)
        response = (
            await self._request_sold_orders(cookie, token, data)
            if token
            else await self._bootstrap_order_session(account_id, cookie, data)
        )
        return _parse_sold_order_page(
            response,
            account_id,
            page_number=page_number,
            page_size=page_size,
        )

    async def fetch_all_sold(
        self,
        account_id: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 100,
    ) -> SoldOrderSnapshot:
        """完整读取卖家订单快照。

        已售订单是历史记录; 与商品镜像不同, 任何分页结果都不会据此删除或取消本地订单。
        """
        if max_pages < 1:
            msg = "max_pages 必须 >= 1"
            raise ValueError(msg)
        orders: list[RemoteSoldOrder] = []
        reported_total = 0
        for page_number in range(1, max_pages + 1):
            page = await self.fetch_sold_page(
                account_id,
                page_number=page_number,
                page_size=page_size,
            )
            orders.extend(page.orders)
            reported_total = page.total_count
            if not page.has_next_page:
                return SoldOrderSnapshot(
                    orders=orders,
                    fetched_at=datetime.now(UTC),
                    pages=page_number,
                    reported_total=reported_total,
                )
            if self._page_delay_s:
                await asyncio.sleep(self._page_delay_s)
        msg = f"订单页数达到安全上限 {max_pages},未写入本地镜像"
        raise OrderSyncError(msg)

    async def _load_order_auth(self, account_id: str) -> str:
        cookie = await self._signer.load_cookie_value(account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise OrderSyncError(msg)
        return cookie

    async def _request_sold_orders(
        self,
        cookie: str,
        token: str,
        data: str,
    ) -> httpx.Response:
        if token:
            signed = make_headers(token, data=data)
            timestamp = signed.x_t
            sign = signed.x_sign
            app_key = signed.app_key
        else:
            timestamp = str(int(time.time() * 1000))
            sign = compute_sign("", int(timestamp), APP_KEY, data)
            app_key = APP_KEY
        params = {
            "jsv": "2.7.2",
            "appKey": app_key,
            "t": timestamp,
            "sign": sign,
            "v": "1.0",
            "type": "json",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": SOLD_ORDERS_API,
            "valueType": "string",
            "sessionOption": "AutoLoginOnly",
        }
        headers = {**_BROWSER_HEADERS, "Cookie": cookie}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                return await client.post(
                    SOLD_ORDERS_URL,
                    params=params,
                    data={"data": data},
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            msg = f"订单列表网络请求失败({type(exc).__name__})"
            raise OrderSyncError(msg) from exc

    async def _bootstrap_order_session(
        self,
        account_id: str,
        cookie: str,
        data: str,
    ) -> httpx.Response:
        """通过只读订单接口尝试补齐 mtop token; 敏感值只在内存中存在。"""
        bootstrap = await self._request_sold_orders(cookie, "", data)
        merged_cookie = _merge_set_cookies(cookie, bootstrap)
        token = extract_mtop_token(merged_cookie)
        if token:
            return await self._request_sold_orders(merged_cookie, token, data)
        if _response_is_success(bootstrap):
            return bootstrap
        msg = f"账号 {account_id} bootstrap 未返回 mtop token,请重新扫码登录"
        raise OrderSyncError(msg)


def _make_sold_orders_payload(*, page_number: int, page_size: int) -> dict[str, Any]:
    return {
        "pageNumber": page_number,
        "rowsPerPage": page_size,
        "orderIds": "",
        "queryCode": "ALL",
        "orderSearchParam": "{}",
    }


def _parse_sold_order_page(
    response: httpx.Response,
    account_id: str,
    *,
    page_number: int,
    page_size: int,
) -> SoldOrderPage:
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        msg = f"订单列表返回非 JSON(HTTP {response.status_code})"
        raise OrderSyncError(msg) from exc
    ret_text = _response_ret_text(body)
    if response.status_code != 200 or not ret_text.startswith("SUCCESS"):
        error_upper = ret_text.upper()
        if any(marker in error_upper for marker in ("TOKEN", "LOGIN", "SESSION", "EXPIRED")):
            msg = f"账号 {account_id} Cookie 已失效,请重新扫码登录"
        else:
            msg = f"订单列表请求失败: {ret_text or f'HTTP {response.status_code}'}"
        raise OrderSyncError(msg)
    data = body.get("data") if isinstance(body, dict) else None
    module = data.get("module") if isinstance(data, dict) else None
    if not isinstance(module, dict):
        msg = "订单列表响应缺少 data.module"
        raise OrderSyncError(msg)
    items = module.get("items", [])
    if not isinstance(items, list):
        msg = "订单列表响应 items 格式异常"
        raise OrderSyncError(msg)
    return SoldOrderPage(
        orders=[order for item in items if (order := _parse_sold_order_item(item)) is not None],
        page_number=page_number,
        page_size=page_size,
        total_count=_parse_nonnegative_int(module.get("totalCount")),
        has_next_page=_parse_bool(module.get("nextPage")),
    )


def _parse_sold_order_item(item: Any) -> RemoteSoldOrder | None:
    if not isinstance(item, dict):
        return None
    common = item.get("commonData")
    if not isinstance(common, dict):
        return None
    order_id = str(common.get("orderId") or "")
    if not order_id:
        return None
    buyer = item.get("buyerInfoVO")
    price = item.get("priceVO")
    buyer = buyer if isinstance(buyer, dict) else {}
    price = price if isinstance(price, dict) else {}
    raw_status = str(common.get("orderStatus") or "")
    in_refund = str(common.get("inRefund") or "").lower() == "true"
    status = "refunded" if in_refund else _STATUS_MAP.get(raw_status, "unknown")
    item_id = str(common.get("itemId") or "") or None
    return RemoteSoldOrder(
        order_id=order_id,
        item_id=item_id,
        buyer_id=str(buyer.get("buyerId") or "") or None,
        buyer_name=str(buyer.get("userNick") or "") or None,
        amount=_parse_amount(price.get("totalPrice")),
        quantity=_parse_positive_int(price.get("buyNum")),
        status=status,
        placed_at=_parse_order_time(common.get("createTime")),
        raw_payload=_safe_raw_payload(common=common, buyer=buyer, price=price),
    )


def _safe_raw_payload(
    *, common: dict[str, Any], buyer: dict[str, Any], price: dict[str, Any]
) -> dict[str, Any]:
    """只保留订单镜像所需字段, 避免把电话、收货地址等响应内容入库。"""
    return {
        "commonData": _pick(
            common, "orderId", "itemId", "orderStatus", "createTime", "inRefund"
        ),
        "buyerInfoVO": _pick(buyer, "buyerId", "userNick"),
        "priceVO": _pick(price, "totalPrice", "buyNum"),
    }


def _pick(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: source[key] for key in keys if key in source}


def _parse_amount(value: Any) -> float:
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def _parse_positive_int(value: Any) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 1
    return parsed if parsed > 0 else 1


def _parse_nonnegative_int(value: Any) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def _parse_bool(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def _parse_order_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        local = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_ORDER_TIME_ZONE
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


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


def _merge_set_cookies(existing_cookie: str, response: httpx.Response) -> str:
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
