"""闲鱼 WebSocket 注册凭据与初始化帧。

协议字段来自公开项目的运行流程观察;实现为本项目自写。Access Token 仅以
Fernet 密文写入 SQLite,日志和异常中不包含 Cookie 或 Token 原文。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from xianyu_agent.db import Account, WsCredential, get_async_session
from xianyu_agent.protocol.signer import (
    APP_KEY,
    CookieSigner,
    compute_sign,
    extract_cookie_field,
    extract_mtop_token,
)
from xianyu_agent.protocol.ws import register as ws_register, sync as ws_sync

IM_APP_KEY = "444e9908a51d1cb236a27862abc769c9"
TOKEN_API = "mtop.taobao.idlemessage.pc.login.token"
TOKEN_URL = f"https://h5api.m.goofish.com/h5/{TOKEN_API}/1.0/"
TOKEN_CACHE_TTL = timedelta(hours=8)

_BROWSER_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


class WsAuthError(RuntimeError):
    """WS 注册凭据获取失败;消息必须保持脱敏。"""


@dataclass(frozen=True)
class WsCredentials:
    access_token: str
    device_id: str
    user_id: str
    expires_at: datetime


@dataclass(frozen=True)
class WsCredentialStatus:
    account_id: str
    exists: bool
    token_cached: bool
    valid: bool
    device_id_masked: str | None
    expires_at: datetime | None


def generate_device_id(user_id: str) -> str:
    """生成协议接受的 UUID 形设备标识,并绑定当前闲鱼 user id。"""
    if not user_id:
        msg = "user_id 不能为空"
        raise ValueError(msg)
    return f"{uuid.uuid4()}-{user_id}"


def build_registration_frame(credentials: WsCredentials) -> dict:
    """构造 WS `/reg` 注册帧。"""
    return ws_register.build_registration_frame(
        credentials,
        app_key=IM_APP_KEY,
        user_agent=_BROWSER_HEADERS["User-Agent"],
        mid_factory=_generate_mid,
    )


# Compatibility aliases while frame construction moves under protocol/ws.
_generate_mid = ws_register.generate_mid
build_sync_frame = ws_sync.build_sync_frame


class WsTokenProvider:
    """低层 WS credential backend。

    Normal ``get_credentials`` calls are read-only and consume only a currently
    prepared durable credential.  Network refresh, Cookie mutation, token-cache
    writes and device-id creation require the explicit ``force_refresh=True``
    mutation intent supplied by the application CredentialSupervisor boundary.
    """

    def __init__(
        self,
        signer: CookieSigner | None = None,
        *,
        timeout_s: float = 30.0,
        client_factory=None,
    ) -> None:
        self._signer = signer or CookieSigner()
        self._timeout = httpx.Timeout(timeout_s)
        self._client_factory = client_factory

    async def get_credentials(
        self, account_id: str, *, force_refresh: bool = False
    ) -> WsCredentials:
        """Load current credentials unless explicit application refresh is requested.

        With ``force_refresh=False`` this method performs no network request and
        no durable mutation.  This is the transport-facing contract used by
        WsClient.  ``force_refresh=True`` is the low-level mutation operation
        consumed by LegacyWsCredentialBackend behind CredentialSupervisor.
        """
        if not force_refresh:
            cached = await self._load_current(account_id)
            if cached is None:
                msg = f"账号 {account_id} 无可用的已准备 WS Token"
                raise WsAuthError(msg)
            return cached

        cookie = await self._signer.load_cookie_value(account_id)
        if not cookie:
            msg = f"账号 {account_id} 无可用 Cookie"
            raise WsAuthError(msg)
        user_id = extract_cookie_field(cookie, "unb") or extract_cookie_field(cookie, "munb")
        if not user_id:
            msg = f"账号 {account_id} Cookie 缺少 unb,请重新扫码登录"
            raise WsAuthError(msg)
        device_id = await self._load_device_id(account_id)
        if device_id is None:
            device_id = generate_device_id(user_id)
            await self._save_device_id(account_id, device_id)

        response, merged_cookie = await self._request_token(cookie, device_id)
        token = _extract_access_token(response)
        if token is None and extract_mtop_token(merged_cookie) and merged_cookie != cookie:
            response, merged_cookie = await self._request_token(merged_cookie, device_id)
            token = _extract_access_token(response)
        if token is None:
            raise WsAuthError(_safe_token_error(account_id, response))

        if merged_cookie != cookie:
            await self._signer.save_cookie(account_id, merged_cookie)
        expires_at = datetime.now(UTC) + TOKEN_CACHE_TTL
        credentials = WsCredentials(token, device_id, user_id, expires_at)
        await self._save_cached(account_id, credentials)
        return credentials

    async def status(self, account_id: str) -> WsCredentialStatus:
        """Return a printable status without exposing token or device identifiers."""
        async with get_async_session() as session:
            row = (
                await session.execute(
                    select(WsCredential)
                    .join(Account, Account.id == WsCredential.account_id)
                    .where(Account.account_id == account_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return WsCredentialStatus(account_id, False, False, False, None, None)
            token_cached = False
            try:
                token_cached = bool(
                    self._signer.fernet.decrypt(row.encrypted_token.encode("utf-8"))
                )
            except Exception:
                token_cached = False
            expires_at = _as_utc(row.expires_at)
            return WsCredentialStatus(
                account_id=account_id,
                exists=True,
                token_cached=token_cached,
                valid=token_cached and expires_at > datetime.now(UTC) + timedelta(minutes=5),
                device_id_masked=_mask_device_id(row.device_id),
                expires_at=expires_at,
            )

    async def _request_token(self, cookie: str, device_id: str) -> tuple[httpx.Response, str]:
        data = json.dumps(
            {"appKey": IM_APP_KEY, "deviceId": device_id},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        token = extract_mtop_token(cookie) or ""
        timestamp = int(time.time() * 1000)
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": str(timestamp),
            "sign": compute_sign(token.split("_", 1)[0] if token else "", timestamp, APP_KEY, data),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": TOKEN_API,
            "sessionOption": "AutoLoginOnly",
        }
        headers = {**_BROWSER_HEADERS, "Cookie": cookie}
        try:
            if self._client_factory is not None:
                client = self._client_factory()
                response = await client.post(
                    TOKEN_URL, params=params, data={"data": data}, headers=headers
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=True
                ) as client:
                    response = await client.post(
                        TOKEN_URL, params=params, data={"data": data}, headers=headers
                    )
        except httpx.HTTPError as exc:
            msg = f"WS Token 网络请求失败({type(exc).__name__})"
            raise WsAuthError(msg) from exc
        return response, _merge_set_cookies(cookie, response)

    async def _load_device_id(self, account_id: str) -> str | None:
        async with get_async_session() as session:
            row = (
                await session.execute(
                    select(WsCredential)
                    .join(Account, Account.id == WsCredential.account_id)
                    .where(Account.account_id == account_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            return row.device_id if row is not None else None

    async def _load_current(self, account_id: str) -> WsCredentials | None:
        """Read one prepared durable credential with zero mutation side effects."""
        async with get_async_session() as session:
            row = (
                await session.execute(
                    select(WsCredential)
                    .join(Account, Account.id == WsCredential.account_id)
                    .where(Account.account_id == account_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None or _as_utc(row.expires_at) <= datetime.now(UTC):
                return None
            try:
                token = self._signer.fernet.decrypt(
                    row.encrypted_token.encode("utf-8")
                ).decode("utf-8")
            except Exception as exc:
                msg = f"账号 {account_id} WS Token 解密失败"
                raise WsAuthError(msg) from exc
            if not token or not row.device_id:
                return None
            cookie = await self._signer.load_cookie_value(account_id)
            user_id = extract_cookie_field(cookie or "", "unb") or extract_cookie_field(
                cookie or "", "munb"
            )
            if not user_id:
                return None
            return WsCredentials(token, row.device_id, user_id, _as_utc(row.expires_at))

    async def _save_cached(self, account_id: str, credentials: WsCredentials) -> None:
        async with get_async_session() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                msg = f"账号 {account_id} 不存在"
                raise WsAuthError(msg)
            row = (
                await session.execute(
                    select(WsCredential).where(WsCredential.account_id == account.id).limit(1)
                )
            ).scalar_one_or_none()
            encrypted = self._signer.fernet.encrypt(
                credentials.access_token.encode("utf-8")
            ).decode("utf-8")
            if row is None:
                row = WsCredential(
                    account_id=account.id,
                    encrypted_token=encrypted,
                    device_id=credentials.device_id,
                    expires_at=credentials.expires_at,
                )
                session.add(row)
            else:
                row.encrypted_token = encrypted
                row.device_id = credentials.device_id
                row.expires_at = credentials.expires_at
            await session.commit()

    async def _save_device_id(self, account_id: str, device_id: str) -> None:
        """首次 token 请求前持久化设备 ID,认证失败重试时也保持同一标识。"""
        async with get_async_session() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.account_id == account_id).limit(1)
                )
            ).scalar_one_or_none()
            if account is None:
                msg = f"账号 {account_id} 不存在"
                raise WsAuthError(msg)
            existing = (
                await session.execute(
                    select(WsCredential).where(WsCredential.account_id == account.id).limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return
            session.add(
                WsCredential(
                    account_id=account.id,
                    encrypted_token=self._signer.fernet.encrypt(b"").decode("utf-8"),
                    device_id=device_id,
                    expires_at=datetime.now(UTC),
                )
            )
            await session.commit()


def _extract_access_token(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return None
    if response.status_code != 200 or not isinstance(body, dict):
        return None
    ret = body.get("ret")
    success = isinstance(ret, list) and any(str(value).startswith("SUCCESS") for value in ret)
    data = body.get("data")
    if success and isinstance(data, dict) and data.get("accessToken"):
        return str(data["accessToken"])
    return None


def _safe_token_error(account_id: str, response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return f"账号 {account_id} WS Token 请求失败(HTTP {response.status_code})"
    ret = body.get("ret") if isinstance(body, dict) else None
    ret_text = str(ret[0]) if isinstance(ret, list) and ret else f"HTTP {response.status_code}"
    return f"账号 {account_id} WS Token 请求失败:{ret_text[:160]}"


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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _mask_device_id(value: str) -> str:
    digest = __import__("hashlib").sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"device#{digest}"
