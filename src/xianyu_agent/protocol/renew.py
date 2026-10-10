"""长登录 Cookie 续期协议。

行为规格来自参考实现 Christ9038/Ydisks-Xianyu-Helper(Apache-2.0)对闲鱼官网 SDK 的观察,
独立用 Python 重写。项目基准(Issue #2「Credential / Cookie 自动续期」)允许这类正常
生命周期维护,边界是:不以规避平台验证的方式强行续期。因此这里只做静默续期——平台要求
人工验证时由上层进入人工处理,不会在这里重试或绕过。

默认路径对齐参考项目的两步流程:

- ``setLoginSettings.do`` 开启长登录并合并响应 Cookie;
- ``queryLoginSettings.do`` 查询最终状态并再次合并响应 Cookie。

显式传入 ``url`` 时仍保留旧的 ``silentHasLogin.do`` 兼容路径。

当前实现仍有边界:

- 不做浏览器 Cookie 作用域快照(我们没有浏览器 Cookie jar),直接使用持久化 Cookie。
- 官方插件在调用前有一段等待窗口,其边界未核对;这里默认不等待,由调用方决定。
- 只转发浏览器标识头(UA 等),不合成设备指纹身份。
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

SILENT_HAS_LOGIN_URL = "https://passport.goofish.com/newlogin/silentHasLogin.do"
HAS_LOGIN_URL = "https://passport.goofish.com/newlogin/hasLogin.do"
SET_LOGIN_SETTINGS_URL = "https://passport.goofish.com/ac/account/setLoginSettings.do"
QUERY_LOGIN_SETTINGS_URL = "https://passport.goofish.com/ac/account/queryLoginSettings.do"
DOCUMENT_REFERER = "https://www.goofish.com/im"
ORIGIN = "https://www.goofish.com"
APP_NAME = "xianyu"
APP_ENTRANCE = "xianyu_sdkSilent"
FROM_SITE = "0"

FATIGUE_COOKIE = "sdkSilent"
HAVANA_EXPIRY_COOKIE = "havana_lgc_exp"
COOKIE3_EXPIRY_COOKIE = "cookie3_bak_exp"

MODE_HAVANA = "havana"
MODE_COOKIE3 = "cookie3"
SKIP_FATIGUE = "fatigue"
SKIP_LONG_LOGIN_EXPIRED = "long_login_expired"
SUCCESS_RESULT_CODE = 100

_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": ORIGIN,
    "Referer": DOCUMENT_REFERER,
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}

# ECMAScript 的空白集合:Number() 与 Date 解析都会先裁掉这些字符。
_JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_HEX_LIKE = re.compile(r"^[+-]?0[xXoObB][0-9a-fA-F]+$")


class RenewError(RuntimeError):
    """续期请求失败(网络、HTTP 或响应不可解析)。"""


def parse_cookie_values(cookie_header: str) -> dict[str, str]:
    """Parse a Cookie header the way the platform's own script does.

    The plugin splits on the semicolon-space separator, then on the equals sign
    keeping only the first two pieces, and lets a later duplicate override an
    earlier one. Reproducing that exactly keeps the mode decision identical to the
    browser's own decision.
    """
    values: dict[str, str] = {}
    if not cookie_header:
        return values
    for part in cookie_header.split("; "):
        pieces = part.split("=")
        values[pieces[0]] = pieces[1] if len(pieces) > 1 else ""
    return values


def javascript_millis(raw: str | None) -> float:
    """Mimic JavaScript's Number() plus Date TimeClip for a cookie expiry value."""
    if raw is None:
        return math.nan
    text = raw.strip(_JS_WHITESPACE)
    if not text or any(token in text for token in ("_", "Inf", "inf")):
        return math.nan
    try:
        number = float(int(text, 0)) if _HEX_LIKE.match(text) else float(text)
    except ValueError:
        return math.nan
    if math.isnan(number) or abs(number) > 8.64e15:
        return math.nan
    return number


def cookie_time_after(raw: str | None, now: datetime) -> bool:
    """Whether a cookie expiry value is still in the future.

    A missing or empty value is not eligible, while a present-but-unparsable value
    is: that split matches the reference implementation and the plugin's own
    negated Number/now comparison.
    """
    if not raw:
        return False
    return not (javascript_millis(raw) <= now.timestamp() * 1000)


def strict_cookie_time_after(raw: str | None, now: datetime) -> bool:
    """Stricter variant used for the fatigue window: unparsable is not eligible."""
    if not raw:
        return False
    return javascript_millis(raw) > now.timestamp() * 1000


def select_renew_mode(
    cookies: Mapping[str, str], *, now: datetime, session_expired: bool = False
) -> tuple[str | None, str | None]:
    """Choose the silent-renewal mode, or explain why it must not be attempted.

    The fatigue reason means the plugin already renewed recently: calling again is
    exactly the hammering that prolongs a risk-control block. The long_login_expired
    reason means the long-login credential is gone, so no request is sent and a
    human must log in again.
    """
    if not session_expired and strict_cookie_time_after(cookies.get(FATIGUE_COOKIE), now):
        return None, SKIP_FATIGUE
    if cookie_time_after(cookies.get(HAVANA_EXPIRY_COOKIE), now):
        return MODE_HAVANA, None
    if cookie_time_after(cookies.get(COOKIE3_EXPIRY_COOKIE), now):
        return MODE_COOKIE3, None
    return None, SKIP_LONG_LOGIN_EXPIRED


def build_silent_renew_params(mode: str) -> list[tuple[str, str]]:
    """Ordered query parameters for one silent-renewal request."""
    params = [
        ("documentReferer", DOCUMENT_REFERER),
        ("appName", APP_NAME),
        ("appEntrance", APP_ENTRANCE),
        ("fromSite", FROM_SITE),
    ]
    if mode == MODE_HAVANA:
        params.append(("ltl", "true"))
    elif mode == MODE_COOKIE3:
        params.extend([("skipSessionFilter", "true"), ("c2r", "true")])
    else:
        msg = f"未知静默续期模式: {mode}"
        raise ValueError(msg)
    return params


def build_renew_headers(cookie_header: str) -> dict[str, str]:
    """Headers for a renewal request; only browser-observed identity is relayed."""
    return {
        **_BROWSER_HEADERS,
        "Cookie": cookie_header.replace("\n", "").replace("\r", ""),
    }


def is_renew_success(body: Mapping[str, object]) -> bool:
    """Whether the platform reported a finished, successful renewal."""
    content = body.get("content")
    data = body.get("data")
    if isinstance(data, Mapping) and data.get("content"):
        content = data["content"]
    if not isinstance(content, Mapping):
        return False
    inner = content.get("data")
    if not isinstance(inner, Mapping):
        return False
    if inner.get("processFinished") is not True:
        return False
    try:
        return int(str(inner.get("resultCode"))) == SUCCESS_RESULT_CODE
    except (TypeError, ValueError):
        return False


def is_long_login_set_success(body: Mapping[str, object]) -> bool:
    """Whether setLoginSettings returned an explicit business success."""
    if body.get("success") is True:
        return True
    data = body.get("data")
    return isinstance(data, Mapping) and data.get("success") is True


def find_return_value(body: Mapping[str, object]) -> Mapping[str, object] | None:
    """Find the nested queryLoginSettings returnValue without trusting shape depth."""
    value = body.get("returnValue")
    if isinstance(value, Mapping):
        return value
    for child in body.values():
        if isinstance(child, Mapping):
            found = find_return_value(child)
            if found is not None:
                return found
    return None


def merge_set_cookies(existing: str, set_cookies: Sequence[str]) -> str:
    """Merge Set-Cookie values into a Cookie header, preserving name order.

    Updates in place (later value wins) and appends names that were not present, so
    the header keeps looking like the browser's own ordering.
    """
    values = parse_cookie_values(existing)
    order = list(values)
    for raw in set_cookies:
        pair = raw.split(";", 1)[0].strip()
        name, separator, value = pair.partition("=")
        if not separator or not name.strip():
            continue
        name = name.strip()
        if name not in values:
            order.append(name)
        values[name] = value.strip()
    return "; ".join(f"{name}={values[name]}" for name in order)


def changed_cookie_names(before: str, after: str) -> tuple[str, ...]:
    """Names whose value changed or appeared; values are never returned."""
    old = parse_cookie_values(before)
    new = parse_cookie_values(after)
    return tuple(sorted(name for name, value in new.items() if old.get(name) != value))


@dataclass(frozen=True)
class RenewOutcome:
    """Result of one renewal attempt."""

    mode: str | None = None
    skipped: bool = False
    skip_reason: str | None = None
    success: bool = False
    cookie: str = ""
    changed_names: tuple[str, ...] = field(default_factory=tuple)
    message: str = ""
    need_human_login: bool = False


class XianyuLongLoginRenewer:
    """Perform one silent long-login renewal for an account cookie."""

    def __init__(
        self,
        *,
        url: str | None = None,
        set_url: str = SET_LOGIN_SETTINGS_URL,
        query_url: str = QUERY_LOGIN_SETTINGS_URL,
        timeout_s: float = 15.0,
        pre_call_delay_seconds: float = 0.0,
    ) -> None:
        self._url = url
        self._set_url = set_url
        self._query_url = query_url
        self._timeout = timeout_s
        self._pre_call_delay = pre_call_delay_seconds

    async def renew(
        self,
        cookie: str,
        *,
        now: datetime | None = None,
        session_expired: bool = False,
    ) -> RenewOutcome:
        """Attempt one silent renewal; never retries on failure."""
        trimmed = (cookie or "").strip()
        if not trimmed:
            return RenewOutcome(cookie="", message="Cookie 为空,无法续期", need_human_login=True)
        observed_at = now or datetime.now(UTC)
        mode, skip_reason = select_renew_mode(
            parse_cookie_values(trimmed), now=observed_at, session_expired=session_expired
        )
        if mode is None:
            return RenewOutcome(
                skipped=True,
                skip_reason=skip_reason,
                cookie=trimmed,
                message=_skip_message(skip_reason),
                need_human_login=skip_reason == SKIP_LONG_LOGIN_EXPIRED,
            )
        if self._pre_call_delay > 0:
            await asyncio.sleep(self._pre_call_delay)
        if self._url is not None:
            payload, set_cookies = await self._call_silent(trimmed, mode)
            merged = merge_set_cookies(trimmed, set_cookies)
            success = is_renew_success(payload)
        else:
            merged, success = await self._call_long_login(trimmed)
        return RenewOutcome(
            mode=mode,
            success=success,
            cookie=merged,
            changed_names=changed_cookie_names(trimmed, merged),
            message="续期成功" if success else "平台未确认续期完成",
            need_human_login=not success,
        )

    async def _call_silent(self, cookie: str, mode: str) -> tuple[Mapping[str, object], list[str]]:
        return await self._post_json(
            self._url or SILENT_HAS_LOGIN_URL,
            cookie,
            params=build_silent_renew_params(mode),
        )

    async def _call_long_login(self, cookie: str) -> tuple[str, bool]:
        set_payload, set_cookies = await self._post_json(
            self._set_url,
            cookie,
            params=[("fromSite", "77"), ("appName", "xianyu"), ("bizEntrance", "web")],
            form={"status": "0"},
        )
        merged = merge_set_cookies(cookie, set_cookies)
        if not is_long_login_set_success(set_payload):
            return merged, False
        query_payload, query_cookies = await self._post_json(
            self._query_url,
            merged,
            params=[("fromSite", "77"), ("appName", "xianyu"), ("bizEntrance", "web")],
        )
        merged = merge_set_cookies(merged, query_cookies)
        value = find_return_value(query_payload)
        if value is None:
            return merged, False
        enabled = value.get("hasLongTokenLogin")
        return merged, enabled is True

    async def _post_json(
        self,
        url: str,
        cookie: str,
        *,
        params: Sequence[tuple[str, str]],
        form: dict[str, str] | None = None,
    ) -> tuple[Mapping[str, object], list[str]]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                response = await client.post(
                    url,
                    params=params,
                    data=form,
                    headers=build_renew_headers(cookie),
                )
        except httpx.HTTPError as exc:
            msg = f"续期网络请求失败({type(exc).__name__})"
            raise RenewError(msg) from exc
        if response.status_code < 200 or response.status_code >= 300:
            msg = f"续期 HTTP 状态异常({response.status_code})"
            raise RenewError(msg)
        set_cookies = response.headers.get_list("set-cookie")
        try:
            body = response.json()
        except ValueError as exc:
            msg = "续期响应不是合法 JSON"
            raise RenewError(msg) from exc
        if not isinstance(body, Mapping):
            msg = "续期响应格式异常"
            raise RenewError(msg)
        return body, set_cookies


def _skip_message(reason: str | None) -> str:
    if reason == SKIP_FATIGUE:
        return "sdkSilent 疲劳窗口内,跳过静默续期"
    if reason == SKIP_LONG_LOGIN_EXPIRED:
        return "长登录凭证已过期,需要人工重新登录"
    return "无需静默续期"
