"""卖家账号资料(mtop.idle.web.user.page.nav)—— 只读。

接口事实来自参考项目 xcai413/Ydisks-Xianyu-Helper(Apache-2.0,
internal/xianyu/mtop/user_profile.go)的只读核对:API 名、1.0 版本、空 ``data={}`` 表单、
查询参数多出 ``ecode=0`` 与 ``spm_cnt=a21ybx.home.0.0``、以及昵称/头像的取值路径
(``data.module.base.displayName``,回退 ``displayNick``,头像取 ``avatar``)。

这是只读接口:不修改任何平台状态,所以校准成本低、风险也低。它解决的是"本地账号昵称一直是空"
这个运营可见性问题。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from xianyu_agent.protocol.mtop.endpoints.items import _BROWSER_HEADERS as BROWSER_HEADERS
from xianyu_agent.protocol.mtop.request_gate import load_request_cookie, observe_mtop_response
from xianyu_agent.protocol.mtop.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    derive_token_seed,
    extract_mtop_token,
)

USER_PAGE_NAV_API = "mtop.idle.web.user.page.nav"
USER_PAGE_NAV_VERSION = "1.0"
USER_PAGE_NAV_URL = f"https://h5api.m.goofish.com/h5/{USER_PAGE_NAV_API}/{USER_PAGE_NAV_VERSION}/"
USER_PAGE_NAV_SPM = "a21ybx.home.0.0"


class UserProfileError(RuntimeError):
    """读取账号资料失败。"""


@dataclass(frozen=True, slots=True)
class UserProfile:
    """卖家账号在平台上展示的昵称与头像。"""

    nickname: str | None = None
    display_nick: str | None = None
    avatar_url: str | None = None


def build_user_profile_data() -> str:
    """The profile endpoint takes an empty JSON body."""
    return "{}"


def build_user_profile_params(
    token: str, data: str, *, timestamp: str | None = None
) -> dict[str, str]:
    """Build the signed query parameters for one profile request."""
    resolved_timestamp = timestamp or str(int(time.time() * 1000))
    sign = compute_sign(derive_token_seed(token), int(resolved_timestamp), APP_KEY, data)
    return {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": resolved_timestamp,
        "sign": sign,
        "v": USER_PAGE_NAV_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": USER_PAGE_NAV_API,
        "sessionOption": "AutoLoginOnly",
        # 参考实现多带的两个参数:ecode 是风控标记,spm_cnt 是页面来源。
        "ecode": "0",
        "spm_cnt": USER_PAGE_NAV_SPM,
    }


def build_user_profile_headers(cookie: str) -> dict[str, str]:
    """Build request headers; Origin/Referer stay on the goofish.com H5 origin."""
    return {**BROWSER_HEADERS, "Cookie": cookie}


def parse_user_profile(payload: Mapping[str, Any]) -> UserProfile:
    """Read nickname/avatar out of the documented response paths."""
    raw_ret = payload.get("ret")
    ret = tuple(str(value) for value in raw_ret) if isinstance(raw_ret, list) else ()
    if ret and not any("SUCCESS" in value for value in ret):
        detail = "; ".join(ret)
        msg = f"读取账号资料失败: {detail}"
        raise UserProfileError(msg)
    data = payload.get("data")
    module = data.get("module") if isinstance(data, Mapping) else None
    base = module.get("base") if isinstance(module, Mapping) else None
    if not isinstance(base, Mapping):
        return UserProfile()

    def _text(value: object) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    display_nick = _text(base.get("displayNick"))
    nickname = _text(base.get("displayName")) or display_nick
    return UserProfile(
        nickname=nickname,
        display_nick=display_nick,
        avatar_url=_text(base.get("avatar")),
    )


class XianyuUserProfileClient:
    """Read the current seller profile; requires an existing signed cookie."""

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        url: str = USER_PAGE_NAV_URL,
        timeout_s: float = 20.0,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._url = url
        self._timeout = timeout_s

    async def fetch_profile(self, account_id: str) -> UserProfile:
        """Fetch the nickname/avatar of the account that owns this cookie."""
        data = build_user_profile_data()
        cookie = await load_request_cookie(self._signer, account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise UserProfileError(msg)
        token = extract_mtop_token(cookie)
        if not token:
            msg = "Cookie 缺少 _m_h5_tk,请先完成只读同步以获取签名 token"
            raise UserProfileError(msg)
        payload = await self._post(account_id, cookie, token, data)
        return parse_user_profile(payload)

    async def _post(self, account_id: str, cookie: str, token: str, data: str) -> Mapping[str, Any]:
        params = build_user_profile_params(token, data)
        headers = build_user_profile_headers(cookie)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    self._url,
                    params=params,
                    content=urlencode({"data": data}),
                    headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            msg = f"账号资料请求失败({type(exc).__name__})"
            raise UserProfileError(msg) from exc
        await observe_mtop_response(account_id, response)
        if response.status_code != 200:
            msg = f"账号资料 HTTP 状态异常: {response.status_code}"
            raise UserProfileError(msg)
        try:
            payload = response.json()
        except ValueError as exc:
            msg = "账号资料响应不是合法 JSON"
            raise UserProfileError(msg) from exc
        if not isinstance(payload, Mapping):  # pragma: no cover - 防御性分支
            msg = "账号资料响应结构异常"
            raise UserProfileError(msg)
        return payload
