"""平台确认发货(mtop.taobao.idle.logistic.consign.dummy)—— 未真实校准。

闲鱼虚拟商品的发货分成两段:把卡密发给买家,以及在订单上确认发货。只做前一段时买家已经
拿到内容,但订单仍停在待发货状态,平台侧按未发货超时处理,并影响后续退款判定。

接口事实来自参考项目 xcai413/Ydisks-Xianyu-Helper(Apache-2.0,
internal/xianyu/mtop/consign.go)的只读核对:API 名、1.0 版本、表单请求体字段
(orderId / tradeText / picList / newUnconsign)、签名参数集合、Referer 与 Origin 指向
www.goofish.com、以及 SUCCESS 成功标记。本文件是独立 Python 实现,未复制其源码。

未真实校准:重复发货时平台返回什么业务错误尚无实证(参考实现只记录过
``FAIL_BIZ_ORDER_STATUS_ERROR::订单状态错误``),因此这里不猜测"已发货"的同义词,
任何非 SUCCESS 都如实上报,由调用方按确定失败或结果不明分别处理。

凭证字段(tradeText / picList)承载发给买家的卡密内容,只参与本次请求,不写入日志、
错误串或任务快照。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from xianyu_agent.protocol.mtop.cookies import scoped_cookie_header
from xianyu_agent.protocol.mtop.endpoints.items import _BROWSER_HEADERS as BROWSER_HEADERS
from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_mtop_token,
)

CONSIGN_API = "mtop.taobao.idle.logistic.consign.dummy"
CONSIGN_VERSION = "1.0"
CONSIGN_URL = f"https://h5api.m.goofish.com/h5/{CONSIGN_API}/{CONSIGN_VERSION}/"


class ConsignError(RuntimeError):
    """确认发货失败。"""


class ConsignRejectedError(ConsignError):
    """平台明确拒绝:请求已到达并给出业务错误,重试不会改变结果。"""


class ConsignUncertainError(ConsignError):
    """请求可能已经到达平台但结果不明(网络中断、5xx、响应不可解析)。"""


@dataclass(frozen=True)
class ConsignResult:
    """一次确认发货请求的结果。"""

    order_id: str
    confirmed: bool
    ret: tuple[str, ...] = ()
    confirmed_at: datetime | None = None


def build_consign_data(
    order_id: str,
    *,
    trade_text: str = "",
    pic_list: Sequence[str] = (),
) -> str:
    """Render the confirm-shipment body for one order.

    ``trade_text`` 与 ``pic_list`` 是发给买家的发货凭证,只作为本次请求的字段,
    调用方不得记录或持久化它们的明文。
    """
    if not order_id.strip():
        msg = "order_id 不能为空"
        raise ValueError(msg)
    payload: dict[str, Any] = {
        "orderId": order_id.strip(),
        "tradeText": trade_text or "",
        "picList": [str(url) for url in pic_list],
        "newUnconsign": True,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_consign_params(token: str, data: str, *, timestamp: str | None = None) -> dict[str, str]:
    """Build the signed query parameters for one confirm-shipment request."""
    resolved_timestamp = timestamp or str(int(time.time() * 1000))
    sign = compute_sign(token, int(resolved_timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": resolved_timestamp,
        "sign": sign,
        "v": CONSIGN_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": CONSIGN_API,
        "sessionOption": "AutoLoginOnly",
    }


def build_consign_headers(cookie: str) -> dict[str, str]:
    """Build request headers; Origin/Referer stay on the goofish.com H5 origin."""
    return {
        **BROWSER_HEADERS,
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Cookie": cookie,
    }


def parse_consign_response(payload: Mapping[str, Any], *, order_id: str) -> ConsignResult:
    """Turn a confirm-shipment response into an outcome.

    平台在 ``ret`` 里给出结果:含 SUCCESS 视为确认成功,其余按业务拒绝抛出
    ``ConsignRejectedError``,由调用方决定是否升级为人工处理。
    """
    raw_ret = payload.get("ret")
    ret = tuple(str(value) for value in raw_ret) if isinstance(raw_ret, list) else ()
    if any("SUCCESS" in value for value in ret):
        return ConsignResult(
            order_id=order_id,
            confirmed=True,
            ret=ret,
            confirmed_at=datetime.now(UTC),
        )
    detail = "; ".join(ret) or "响应缺少 ret 字段"
    msg = f"确认发货被拒绝: {detail}"
    raise ConsignRejectedError(msg)


class XianyuConsignClient:
    """Confirm one order's shipment; requires an existing signed cookie."""

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        url: str = CONSIGN_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._url = url
        self._timeout = timeout_s

    async def confirm_delivery(
        self,
        account_id: str,
        order_id: str,
        *,
        trade_text: str = "",
        pic_list: Sequence[str] = (),
    ) -> ConsignResult:
        """Confirm one order as shipped, optionally carrying the delivery credential."""
        data = build_consign_data(order_id, trade_text=trade_text, pic_list=pic_list)
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise ConsignError(msg)
        load_snapshot = getattr(self._signer, "load_cookie_snapshot", None)
        snapshot = await load_snapshot(account_id) if callable(load_snapshot) else None
        signing_cookie = cookie
        request_cookie = cookie
        if snapshot:
            signing_cookie = scoped_cookie_header(
                snapshot, "https://www.goofish.com/", document_cookie_only=True
            )
            request_cookie = scoped_cookie_header(snapshot, self._url, document_cookie_only=False)
        token = extract_mtop_token(signing_cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise ConsignError(msg)
        payload = await self._post(account_id, request_cookie, token, data)
        return parse_consign_response(payload, order_id=order_id.strip())

    async def _post(self, account_id: str, cookie: str, token: str, data: str) -> Mapping[str, Any]:
        params = build_consign_params(token, data)
        headers = build_consign_headers(cookie)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    self._url,
                    params=params,
                    content=urlencode({"data": data}),
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            # 请求可能已经到达平台:按结果不明上报,不自动重试。
            msg = f"确认发货网络请求失败,结果不明({type(exc).__name__})"
            raise ConsignUncertainError(msg) from exc
        await observe_mtop_response(account_id, response)
        if response.status_code >= 500:
            msg = f"确认发货服务异常,结果不明(HTTP {response.status_code})"
            raise ConsignUncertainError(msg)
        try:
            payload = response.json()
        except ValueError as exc:
            msg = f"确认发货响应不是合法 JSON,结果不明(HTTP {response.status_code})"
            raise ConsignUncertainError(msg) from exc
        if not isinstance(payload, Mapping):  # pragma: no cover - 防御性分支
            msg = "确认发货响应结构异常"
            raise ConsignUncertainError(msg)
        return payload
