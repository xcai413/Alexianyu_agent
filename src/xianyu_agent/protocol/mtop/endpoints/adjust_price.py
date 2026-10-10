"""订单改价(mtop.taobao.idle.trade.user.adjust.price)—— 未真实校准。

平台只接受买家**尚未付款**的订单改价;改价后买家按新价格付款。

接口事实来自参考项目 xcai413/Ydisks-Xianyu-Helper(Apache-2.0,
internal/xianyu/mtop/adjust_price.go)的只读核对:API 名、1.0 版本、POST 表单请求体
(orderId / modifyFee 分为单位的整数 / newTransportFee)、签名参数集合、以及
``ret`` 含 SUCCESS 且 ``data.success`` 为真才算成功。本文件是独立 Python 实现,
未复制其源码。

金额只用整数分参与协议:浮点数在金额上没有可接受的误差。真实调用前必须先做单订单校准,
详见 docs/待真实验证清单.md。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx

from xianyu_agent.protocol.mtop.endpoints.items import _BROWSER_HEADERS as BROWSER_HEADERS
from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_mtop_token,
)

ADJUST_PRICE_API = "mtop.taobao.idle.trade.user.adjust.price"
ADJUST_PRICE_VERSION = "1.0"
ADJUST_PRICE_URL = f"https://h5api.m.goofish.com/h5/{ADJUST_PRICE_API}/{ADJUST_PRICE_VERSION}/"
MAX_PRICE_CENTS = 100_000_000  # 单笔 100 万元上限,超过几乎一定是误操作


class AdjustPriceError(RuntimeError):
    """改价失败。"""


class AdjustPriceRejectedError(AdjustPriceError):
    """平台明确拒绝(订单状态或权限不允许),重试不会改变结果。"""


class AdjustPriceUncertainError(AdjustPriceError):
    """请求可能已经到达平台但结果不明(网络中断、5xx、响应不可解析)。"""


@dataclass(frozen=True)
class AdjustPriceResult:
    """一次改价请求的结果。"""

    order_id: str
    price_cents: int
    modified: bool
    ret: tuple[str, ...] = ()
    adjusted_at: datetime | None = None


def parse_price_to_cents(value: str | Decimal | int) -> int:
    """把"元"表示的金额转成整数分,最多两位小数。

    用 Decimal 而不是 float:金额换算不接受二进制浮点误差。
    """
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        msg = f"金额格式不合法: {value!r}"
        raise ValueError(msg) from exc
    if amount <= 0:
        msg = "改价金额必须大于 0"
        raise ValueError(msg)
    cents = (amount * 100).to_integral_value()
    if Decimal(cents) != amount * 100:
        msg = "改价金额最多两位小数(到分)"
        raise ValueError(msg)
    resolved = int(cents)
    if resolved > MAX_PRICE_CENTS:
        msg = f"改价金额超过上限 {MAX_PRICE_CENTS / 100:.2f} 元"
        raise ValueError(msg)
    return resolved


def build_adjust_price_data(order_id: str, price_cents: int, *, transport_fee: str = "0") -> str:
    """Render the adjust-price body for one order."""
    if not order_id.strip():
        msg = "order_id 不能为空"
        raise ValueError(msg)
    if not isinstance(price_cents, int) or price_cents <= 0:
        msg = "price_cents 必须是正整数(单位:分)"
        raise ValueError(msg)
    payload = {
        "modifyFee": price_cents,
        "newTransportFee": str(transport_fee),
        "orderId": order_id.strip(),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_adjust_price_params(
    token: str, data: str, *, timestamp: str | None = None
) -> dict[str, str]:
    """Build the signed query parameters for one adjust-price request."""
    resolved_timestamp = timestamp or str(int(time.time() * 1000))
    sign = compute_sign(token, int(resolved_timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": resolved_timestamp,
        "sign": sign,
        "v": ADJUST_PRICE_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": ADJUST_PRICE_API,
        "sessionOption": "AutoLoginOnly",
    }


def build_adjust_price_headers(cookie: str) -> dict[str, str]:
    """Build request headers; Origin/Referer stay on the goofish.com H5 origin."""
    return {**BROWSER_HEADERS, "Cookie": cookie}


def parse_adjust_price_response(
    payload: Mapping[str, Any], *, order_id: str, price_cents: int
) -> AdjustPriceResult:
    """Turn an adjust-price response into an outcome.

    参考实现要求 ``ret`` 含 SUCCESS **且** ``data.success`` 为真:只看 ret 会把
    "接口通了但改价没生效"误判成成功。
    """
    raw_ret = payload.get("ret")
    ret = tuple(str(value) for value in raw_ret) if isinstance(raw_ret, list) else ()
    data = payload.get("data")
    success_flag = bool(isinstance(data, Mapping) and data.get("success") is True)
    if any("SUCCESS" in value for value in ret) and success_flag:
        return AdjustPriceResult(
            order_id=order_id,
            price_cents=price_cents,
            modified=True,
            ret=ret,
            adjusted_at=datetime.now(UTC),
        )
    detail = "; ".join(ret) or "响应缺少 ret 字段"
    if any("SUCCESS" in value for value in ret) and not success_flag:
        detail = f"{detail}(接口返回成功但 data.success 不是 true)"
    msg = f"改价被拒绝: {detail}"
    raise AdjustPriceRejectedError(msg)


class XianyuAdjustPriceClient:
    """Adjust one unpaid order's price; requires an existing signed cookie."""

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        url: str = ADJUST_PRICE_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._url = url
        self._timeout = timeout_s

    async def adjust_order_price(
        self, account_id: str, order_id: str, price_cents: int
    ) -> AdjustPriceResult:
        """Change the total price of one unpaid order, in cents."""
        data = build_adjust_price_data(order_id, price_cents)
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise AdjustPriceError(msg)
        token = extract_mtop_token(cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise AdjustPriceError(msg)
        payload = await self._post(account_id, cookie, token, data)
        return parse_adjust_price_response(
            payload, order_id=order_id.strip(), price_cents=price_cents
        )

    async def _post(self, account_id: str, cookie: str, token: str, data: str) -> Mapping[str, Any]:
        params = build_adjust_price_params(token, data)
        headers = build_adjust_price_headers(cookie)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    self._url,
                    params=params,
                    content=urlencode({"data": data}),
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            msg = f"改价网络请求失败,结果不明({type(exc).__name__})"
            raise AdjustPriceUncertainError(msg) from exc
        await observe_mtop_response(account_id, response)
        if response.status_code >= 500:
            msg = f"改价服务异常,结果不明(HTTP {response.status_code})"
            raise AdjustPriceUncertainError(msg)
        try:
            payload = response.json()
        except ValueError as exc:
            msg = f"改价响应不是合法 JSON,结果不明(HTTP {response.status_code})"
            raise AdjustPriceUncertainError(msg) from exc
        if not isinstance(payload, Mapping):  # pragma: no cover - 防御性分支
            msg = "改价响应结构异常"
            raise AdjustPriceUncertainError(msg)
        return payload
