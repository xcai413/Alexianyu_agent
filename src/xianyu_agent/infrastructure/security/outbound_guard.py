"""出站请求守卫(Issue #5 Phase 6, §19/M54)。

发货 API 的地址可能来自运营配置(甚至来自被攻破的配置或重定向),所以每次出站都要重新检查:
只允许 https;拒绝本机/内网/链路本地/云元数据等目标;重定向后的地址同样要过一遍,不能只验证
初始 URL。默认拒绝,只有显式列在 allow_hosts 里的主机才放行(给单机部署留出口)。

域名解析失败视为不可访问(而不是"当作公网")。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES: tuple[str, ...] = ("https",)
BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
METADATA_IPS: frozenset[str] = frozenset({"169.254.169.254", "fd00:ec2::254"})


class OutboundBlocked(RuntimeError):
    """出站目标被安全策略拒绝。"""


@dataclass(frozen=True)
class UrlCheck:
    allowed: bool
    reason: str


def _is_blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if str(ip) in METADATA_IPS:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def check_url(  # noqa: PLR0911 - 安全检查逐条给理由,提前返回最清楚
    url: str,
    *,
    allow_hosts: tuple[str, ...] = (),
    resolve_dns: bool = True,
) -> UrlCheck:
    """Validate one outbound URL; everything unknown is refused."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ALLOWED_SCHEMES:
        return UrlCheck(
            False, f"只允许 {'/'.join(ALLOWED_SCHEMES)} 出站,当前是 {parts.scheme or '空'}"
        )
    host = (parts.hostname or "").strip().lower()
    if not host:
        return UrlCheck(False, "出站地址缺少主机名")
    if host in allow_hosts:
        return UrlCheck(True, "主机在显式允许列表里")
    if host in BLOCKED_HOSTNAMES:
        return UrlCheck(False, f"主机 {host} 属于本机或元数据服务,不允许出站")
    if _is_blocked_ip(host):
        return UrlCheck(False, f"地址 {host} 属于内网/回环/链路本地,不允许出站")
    if not resolve_dns:
        return UrlCheck(True, "未做域名解析检查")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return UrlCheck(False, f"域名 {host} 解析失败({type(exc).__name__})")
    for info in infos:
        address = str(info[4][0])
        if _is_blocked_ip(address):
            return UrlCheck(False, f"域名 {host} 解析到内网地址 {address},不允许出站")
    return UrlCheck(True, "出站地址通过检查")


def require_url(url: str, *, allow_hosts: tuple[str, ...] = (), resolve_dns: bool = True) -> None:
    """Raise OutboundBlocked when the URL may not be requested."""
    check = check_url(url, allow_hosts=allow_hosts, resolve_dns=resolve_dns)
    if not check.allowed:
        raise OutboundBlocked(check.reason)
