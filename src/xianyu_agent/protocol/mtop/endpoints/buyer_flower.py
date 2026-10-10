"""向买家索要小红花(mtop.taobao.idlemessage.red.flower)—— 未真实校准。

接口名、版本、请求体与 Referer 来自单一来源的只读核对:
xcai413/xianyu-super-butler(AGPL-3.0, utils/xianyu_seller_api.py 的 require_flower
与 app/reply_server.py 的调用点)。本文件为独立 Python 实现,未复制其源码。

证据强度:只有一处来源,没有第二个参考佐证,也没有真实账号校准。因此本模块只提供
"单订单显式触发"的能力,不接入任何定时或批量自动化;首次调用必须由运营在测试订单上
执行并把结果写进 docs/验证记录.md。

已知业务结论:交易完结后即可调用,与评价状态无关;它会向买家真实发送一条消息,因此
同一订单重复调用会重复打扰买家,调用方必须自己按订单幂等。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_mtop_token,
)

FLOWER_API = "mtop.taobao.idlemessage.red.flower"
FLOWER_VERSION = "1.0"
FLOWER_URL = f"https://h5api.m.goofish.com/h5/{FLOWER_API}/{FLOWER_VERSION}/"
SELLER_ORIGIN = "https://seller.goofish.com"
SELLER_SPM = "a21107h.42826273.0.0"
SELLER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


class BuyerFlowerError(RuntimeError):
    """求花失败(凭证、网络或平台业务错误)。"""


@dataclass(frozen=True)
class FlowerResult:
    """一次求花请求的结果。"""

    order_id: str
    requested: bool
    ret: tuple[str, ...] = ()
    requested_at: datetime | None = None


def build_flower_data(order_id: str) -> str:
    """Render the request body for one order."""
    if not order_id.strip():
        msg = "order_id 不能为空"
        raise ValueError(msg)
    return json.dumps({"orderId": order_id.strip()}, ensure_ascii=False, separators=(",", ":"))


def build_flower_params(token: str, data: str, *, timestamp: str | None = None) -> dict[str, str]:
    """Build the signed query parameters for one flower request.

    The reference sends no valueType for this endpoint, so neither do we.
    """
    resolved_timestamp = timestamp or str(int(time.time() * 1000))
    sign = compute_sign(token, int(resolved_timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": resolved_timestamp,
        "sign": sign,
        "v": FLOWER_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": FLOWER_API,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": SELLER_SPM,
    }


def build_flower_headers(cookie: str) -> dict[str, str]:
    """Seller-side headers: Origin/Referer must point at seller.goofish.com."""
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": SELLER_ORIGIN,
        "Referer": f"{SELLER_ORIGIN}/",
        "User-Agent": SELLER_USER_AGENT,
        "Cookie": cookie,
    }


def parse_flower_response(payload: Mapping[str, Any], *, order_id: str) -> FlowerResult:
    """Turn a flower response into an outcome, failing closed on business errors."""
    raw_ret = payload.get("ret")
    ret = tuple(str(value) for value in raw_ret) if isinstance(raw_ret, list) else ()
    if any("SUCCESS" in value for value in ret):
        return FlowerResult(
            order_id=order_id,
            requested=True,
            ret=ret,
            requested_at=datetime.now(UTC),
        )
    detail = "; ".join(ret) or "响应缺少 ret 字段"
    msg = f"求花失败: {detail}"
    raise BuyerFlowerError(msg)


class XianyuBuyerFlowerClient:
    """Ask the buyer for a rating; requires an existing signed cookie."""

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        url: str = FLOWER_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._url = url
        self._timeout = timeout_s

    async def request_flower(self, account_id: str, order_id: str) -> FlowerResult:
        """Request one rating prompt for one order of one account."""
        data = build_flower_data(order_id)
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise BuyerFlowerError(msg)
        token = extract_mtop_token(cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise BuyerFlowerError(msg)
        payload = await self._post(account_id, cookie, token, data)
        return parse_flower_response(payload, order_id=order_id.strip())

    async def _post(self, account_id: str, cookie: str, token: str, data: str) -> Mapping[str, Any]:
        params = build_flower_params(token, data)
        headers = build_flower_headers(cookie)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    self._url,
                    params=params,
                    content=urlencode({"data": data}),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            msg = f"求花网络请求失败({type(exc).__name__})"
            raise BuyerFlowerError(msg) from exc
        await observe_mtop_response(account_id, response)
        if response.status_code != 200:
            msg = f"求花 HTTP 状态异常: {response.status_code}"
            raise BuyerFlowerError(msg)
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - 平台返回非 JSON 时
            msg = "求花响应不是合法 JSON"
            raise BuyerFlowerError(msg) from exc
        if not isinstance(payload, Mapping):  # pragma: no cover - 防御性分支
            msg = "求花响应结构异常"
            raise BuyerFlowerError(msg)
        return payload
