"""闲鱼扫码登录(自写实现,流程参考公开项目的接口字段命名,不复制代码)。

流程(字段见 docs/protocol-notes.md):
  1. GET  h5api.m.goofish.com .../index.get/1.0/  -> 取 m_h5_tk,md5 签名
  2. GET  passport.goofish.com/mini_login.htm     -> 提取 window.viewData.loginFormData
  3. GET  passport.goofish.com/newlogin/qrcode/generate.do -> codeContent(t/ck)
  4. POST passport.goofish.com/newlogin/qrcode/query.do    -> 轮询 qrCodeStatus
  5. CONFIRMED 时响应 Set-Cookie 含 unb 等 -> 拼 Cookie 串

本模块只负责 HTTP 流程与状态机;二维码渲染与 Cookie 落库由 CLI 层负责。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from random import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PASSPORT_HOST = "https://passport.goofish.com"
H5API_INDEX = "https://h5api.m.goofish.com/h5/mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get/1.0/"
API_MINI_LOGIN = f"{PASSPORT_HOST}/mini_login.htm"
API_GENERATE_QR = f"{PASSPORT_HOST}/newlogin/qrcode/generate.do"
API_SCAN_STATUS = f"{PASSPORT_HOST}/newlogin/qrcode/query.do"

APP_KEY = "34839810"
SESSION_TTL_S = 300
POLL_INTERVAL_S = 0.8

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://passport.goofish.com/",
    "Origin": "https://passport.goofish.com",
}


class QrLoginError(RuntimeError):
    """扫码登录流程错误(参数/二维码/风控等)。"""


def _extract_set_cookies(resp: httpx.Response) -> dict[str, str]:
    """从响应头手动解析 set-cookie(不依赖 resp.cookies,兼容 respx/httpx 差异)。"""
    out: dict[str, str] = {}
    for name, value in resp.headers.multi_items():
        if name.lower() == "set-cookie":
            part = value.split(";", 1)[0]
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
    return out


class QrStatus:
    WAITING = "waiting"
    SCANNED = "scanned"
    CONFIRMED = "confirmed"
    SUCCESS = "success"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    VERIFICATION_REQUIRED = "verification_required"
    NOT_FOUND = "not_found"


@dataclass
class QRLoginSession:
    """一次扫码登录会话的状态容器。"""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = QrStatus.WAITING
    qr_content: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    unb: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    verification_url: str | None = None
    created_time: float = field(default_factory=time.time)
    ttl_s: int = SESSION_TTL_S

    def is_expired(self) -> bool:
        return time.time() - self.created_time > self.ttl_s

    def cookie_string(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


class QRLoginClient:
    """扫码登录 HTTP 客户端(纯流程,无 UI)。"""

    def __init__(self, *, timeout_s: float = 30.0) -> None:
        self._timeout = httpx.Timeout(timeout_s)
        self._headers = dict(BROWSER_HEADERS)

    async def generate(self) -> QRLoginSession:
        """执行前三步,返回含 qr_content 的会话。"""
        session = QRLoginSession()
        await self._bootstrap_mh5tk(session)
        await self._fetch_login_params(session)
        await self._fetch_qr_code(session)
        return session

    async def poll(self, session: QRLoginSession) -> str:
        """单次轮询,更新会话状态/cookie;返回最新状态。"""
        if session.status in {
            QrStatus.SUCCESS,
            QrStatus.EXPIRED,
            QrStatus.CANCELLED,
            QrStatus.VERIFICATION_REQUIRED,
        }:
            return session.status
        async with self._client(session) as client:
            resp = await client.post(API_SCAN_STATUS, data=session.params, headers=self._headers)
            _update_session_cookies(session, client, resp)
        try:
            data = resp.json().get("content", {}).get("data", {})
        except json.JSONDecodeError:
            logger.warning("query.do 返回非 JSON: %s", resp.text[:120])
            return session.status
        code = str(data.get("qrCodeStatus") or "")
        if code == "CONFIRMED":
            if data.get("iframeRedirect") is True:
                session.status = QrStatus.VERIFICATION_REQUIRED
                session.verification_url = data.get("iframeRedirectUrl")
            else:
                session.status = QrStatus.SUCCESS
                session.unb = session.cookies.get("unb")
        elif code == "SCANED":
            session.status = QrStatus.SCANNED
        elif code == "EXPIRED":
            session.status = QrStatus.EXPIRED
        elif code == "NEW":
            pass  # 仍在等待
        else:
            session.status = QrStatus.CANCELLED
        return session.status

    async def wait_for_login(
        self,
        session: QRLoginSession,
        *,
        timeout_s: float = SESSION_TTL_S,
        on_status=None,
    ) -> str:
        """轮询直到终态(成功/过期/取消/风控)或超时。"""
        start = time.time()
        while time.time() - start < timeout_s:
            status = await self.poll(session)
            if on_status is not None:
                on_status(status)
            if status in {
                QrStatus.SUCCESS,
                QrStatus.EXPIRED,
                QrStatus.CANCELLED,
                QrStatus.VERIFICATION_REQUIRED,
            }:
                return status
            await asyncio.sleep(POLL_INTERVAL_S)
        if session.status not in {QrStatus.SUCCESS, QrStatus.VERIFICATION_REQUIRED}:
            session.status = QrStatus.EXPIRED
        return session.status

    # ---- 内部三步 ----

    def _client(self, session: QRLoginSession) -> Any:
        """构造带会话 cookie 的 AsyncClient(httpx 0.28 起 per-request cookies 已弃用)。"""
        return httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            cookies=session.cookies,
        )

    async def _bootstrap_mh5tk(self, session: QRLoginSession) -> None:
        """第一步:拿 m_h5_tk 并完成一次签名请求。"""
        async with self._client(session) as client:
            resp = await client.get(H5API_INDEX, headers=self._headers)
            _update_session_cookies(session, client, resp)
            m_h5_tk = session.cookies.get("m_h5_tk", "")
            token = m_h5_tk.split("_", 1)[0] if "_" in m_h5_tk else ""
            data_str = json.dumps({"bizScene": "home"}, separators=(",", ":"))
            t = str(int(time.time() * 1000))
            sign = hashlib.md5(f"{token}&{t}&{APP_KEY}&{data_str}".encode()).hexdigest()
            params = {
                "jsv": "2.7.2",
                "appKey": APP_KEY,
                "t": t,
                "sign": sign,
                "v": "1.0",
                "type": "originaljson",
                "dataType": "json",
                "timeout": 20000,
                "api": "mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get",
                "data": data_str,
            }
            signed_response = await client.post(H5API_INDEX, params=params, headers=self._headers)
            _update_session_cookies(session, client, signed_response)

    async def _fetch_login_params(self, session: QRLoginSession) -> None:
        """第二步:从 mini_login.htm 提取 loginFormData。"""
        params = {
            "lang": "zh_cn",
            "appName": "xianyu",
            "appEntrance": "web",
            "styleType": "vertical",
            "bizParams": "",
            "notLoadSsoView": False,
            "notKeepLogin": False,
            "isMobile": False,
            "qrCodeFirst": False,
            "stie": 77,
            "rnd": random(),
        }
        async with self._client(session) as client:
            resp = await client.get(API_MINI_LOGIN, params=params, headers=self._headers)
            _update_session_cookies(session, client, resp)
        match = re.search(r"window\.viewData\s*=\s*(\{.*?\});", resp.text)
        if not match:
            msg = "获取登录参数失败(未找到 viewData)"
            raise QrLoginError(msg)
        view_data = json.loads(match.group(1))
        data = view_data.get("loginFormData")
        if not data:
            msg = "获取登录参数失败(未找到 loginFormData)"
            raise QrLoginError(msg)
        data["umidTag"] = "SERVER"
        session.params.update(data)

    async def _fetch_qr_code(self, session: QRLoginSession) -> None:
        """第三步:generate.do 出二维码内容。"""
        async with self._client(session) as client:
            resp = await client.get(API_GENERATE_QR, params=session.params, headers=self._headers)
            _update_session_cookies(session, client, resp)
        try:
            content = resp.json().get("content", {})
        except json.JSONDecodeError:
            msg = f"二维码接口返回异常: {resp.text[:120]}"
            raise QrLoginError(msg) from None
        if content.get("success") is not True:
            msg = f"获取登录二维码失败: {content}"
            raise QrLoginError(msg)
        data = content.get("data", {})
        session.params.update({"t": data.get("t"), "ck": data.get("ck")})
        session.qr_content = data.get("codeContent")
        if not session.qr_content:
            msg = "获取登录二维码失败(无 codeContent)"
            raise QrLoginError(msg)
        session.status = QrStatus.WAITING


def _update_session_cookies(
    session: QRLoginSession, client: httpx.AsyncClient, response: httpx.Response
) -> None:
    """Carry every intermediate/redirect Cookie into the next QR-login request."""
    for item in [*response.history, response]:
        session.cookies.update(_extract_set_cookies(item))
    for cookie in client.cookies.jar:
        session.cookies[cookie.name] = cookie.value
