"""商品擦亮(mtop.taobao.idle.item.polish)—— 未真实校准。

接口名、版本(2.0)、请求体、Referer 与「当天已擦亮」的业务错误特征来自两个参考项目的
一致观察:xcai413/Ydisks-Xianyu-Helper(Apache-2.0, internal/xianyu/mtop/account_tasks.go)
与 xcai413/xianyu-super-butler(AGPL-3.0, utils/item_polish.py);本文件为独立 Python 实现,
未复制其源码。

关键事实:
- 该接口是 H5 2.0,传 1.0 会被判「接口不存在」;
- Referer / Origin 指向 www.goofish.com;
- 每个商品每天可擦亮次数由平台限制,重复调用返回业务错误而不是抛异常;
- 缺少签名 token 时直接失败,不发起未签名请求。

未真实校准:本条链路必须在真实账号上先做单商品验证,再把结论写进 docs/验证记录.md。
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

from xianyu_agent.protocol.mtop.endpoints.items import _BROWSER_HEADERS as BROWSER_HEADERS
from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_mtop_token,
)

POLISH_API = "mtop.taobao.idle.item.polish"
POLISH_VERSION = "2.0"
POLISH_URL = f"https://h5api.m.goofish.com/h5/{POLISH_API}/{POLISH_VERSION}/"
POLISH_SPM = "a21ybx.item.0.0"

# 参考实现观察到的「当天已擦亮」信号:命中任一即视为幂等成功而不是失败。
DUPLICATE_POLISH_MARKERS = (
    "IDLEITEM_POLISH_AGAIN",
    "POLISH_DUPLICATE",
    "已经擦亮",
    "一天只能擦亮一次",
)


class ItemPolishError(RuntimeError):
    """擦亮失败(凭证、网络或平台业务错误)。"""


@dataclass(frozen=True)
class PolishResult:
    """擦亮一个商品的结果。

    ``already_polished`` 表示平台告知当天已擦亮,调用方应把它当成成功而不是失败,
    否则重试会把幂等状态误报成故障。
    """

    item_id: str
    polished: bool
    already_polished: bool = False
    ret: tuple[str, ...] = ()
    polished_at: datetime | None = None


def build_polish_data(item_id: str) -> str:
    """Render the polish request body for one item."""
    if not item_id.strip():
        msg = "item_id 不能为空"
        raise ValueError(msg)
    return json.dumps({"itemId": item_id.strip()}, ensure_ascii=False, separators=(",", ":"))


def build_polish_params(token: str, data: str, *, timestamp: str | None = None) -> dict[str, str]:
    """Build the signed query parameters for one polish request."""
    resolved_timestamp = timestamp or str(int(time.time() * 1000))
    sign = compute_sign(token, int(resolved_timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": resolved_timestamp,
        "sign": sign,
        "v": POLISH_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": POLISH_API,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": POLISH_SPM,
    }


def build_polish_headers(cookie: str) -> dict[str, str]:
    """Build request headers; Origin/Referer stay on the goofish.com H5 origin."""
    return {**BROWSER_HEADERS, "Cookie": cookie}


def is_duplicate_polish(ret: Sequence[str]) -> bool:
    """Whether a platform response means \"already polished today\"."""
    joined = "; ".join(str(value) for value in ret)
    return any(marker in joined for marker in DUPLICATE_POLISH_MARKERS)


def parse_polish_response(payload: Mapping[str, Any], *, item_id: str) -> PolishResult:
    """Turn a polish response into an outcome.

    Raises ``ItemPolishError`` for any business error that is not the documented
    \"already polished\" idempotent signal.
    """
    raw_ret = payload.get("ret")
    ret = tuple(str(value) for value in raw_ret) if isinstance(raw_ret, list) else ()
    if any("SUCCESS" in value for value in ret):
        return PolishResult(
            item_id=item_id,
            polished=True,
            ret=ret,
            polished_at=datetime.now(UTC),
        )
    if is_duplicate_polish(ret):
        return PolishResult(item_id=item_id, polished=False, already_polished=True, ret=ret)
    detail = "; ".join(ret) or "响应缺少 ret 字段"
    msg = f"擦亮失败: {detail}"
    raise ItemPolishError(msg)


class XianyuItemPolishClient:
    """Polish one item; requires an existing signed cookie."""

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        url: str = POLISH_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._url = url
        self._timeout = timeout_s

    async def polish_item(self, account_id: str, item_id: str) -> PolishResult:
        """Polish one mirrored item of one account."""
        data = build_polish_data(item_id)
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise ItemPolishError(msg)
        token = extract_mtop_token(cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise ItemPolishError(msg)
        payload = await self._post(account_id, cookie, token, data)
        return parse_polish_response(payload, item_id=item_id.strip())

    async def _post(self, account_id: str, cookie: str, token: str, data: str) -> Mapping[str, Any]:
        params = build_polish_params(token, data)
        headers = build_polish_headers(cookie)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    self._url,
                    params=params,
                    content=urlencode({"data": data}),
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            msg = f"商品擦亮网络请求失败({type(exc).__name__})"
            raise ItemPolishError(msg) from exc
        await observe_mtop_response(account_id, response)
        if response.status_code != 200:
            msg = f"商品擦亮 HTTP 状态异常: {response.status_code}"
            raise ItemPolishError(msg)
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - 平台返回非 JSON 时
            msg = "商品擦亮响应不是合法 JSON"
            raise ItemPolishError(msg) from exc
        if not isinstance(payload, Mapping):  # pragma: no cover - 防御性分支
            msg = "商品擦亮响应结构异常"
            raise ItemPolishError(msg)
        return payload
