"""通知出站:把一条已构造好的请求送出去,并把失败收敛成返回值。

通知是辅助能力,任何失败都不允许抛给调用方的主循环,所以这里只返回 `SendResult`。地址每次
出站前都要过一遍守卫:渠道地址来自运营配置,配置被改坏或被重定向都不应该变成 SSRF 入口。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from xianyu_agent.domain.notify import NotificationChannel, NotifyMessage, build_request
from xianyu_agent.infrastructure.security.outbound_guard import check_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SendResult:
    """一次渠道投递的结果:成功、失败原因,或因为地址不合格而没发。"""

    ok: bool
    error: str | None = None


class ChannelSender:
    """一次投递的 HTTP 边界;可注入 transport 以便离线测试。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_hosts: tuple[str, ...] = (),
        resolve_dns: bool = True,
    ) -> None:
        self._timeout = timeout_seconds
        self._transport = transport
        self._allow_hosts = allow_hosts
        self._resolve_dns = resolve_dns

    async def send(
        self,
        channel: NotificationChannel,
        message: NotifyMessage,
        *,
        secret: str | None = None,
        now: datetime | None = None,
    ) -> SendResult:
        try:
            request = build_request(channel, message, secret=secret, now=now or datetime.now(UTC))
        except ValueError as exc:
            # 渠道配置不合法(比如 bark 缺设备密钥)不该让整轮派发崩掉。
            return SendResult(ok=False, error=f"CONFIG:{type(exc).__name__}")
        check = check_url(request.url, allow_hosts=self._allow_hosts, resolve_dns=self._resolve_dns)
        if not check.allowed:
            logger.warning("notification outbound blocked: %s", check.reason)
            return SendResult(ok=False, error="BLOCKED:outbound_guard")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    request.method, request.url, json=request.payload, headers=request.headers
                )
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"HTTP:{type(exc).__name__}")
        if response.status_code >= 400:
            return SendResult(ok=False, error=f"HTTP {response.status_code}")
        return SendResult(ok=True)
