"""按浏览器作用域筛选 MTOP Cookie 快照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BrowserCookie:
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: float | None = None
    http_only: bool = False
    secure: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "expires": self.expires,
            "http_only": self.http_only,
            "secure": self.secure,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> BrowserCookie:
        return cls(
            name=str(value.get("name") or ""),
            value=str(value.get("value") or ""),
            domain=str(value.get("domain") or ""),
            path=str(value.get("path") or "/"),
            expires=float(value["expires"]) if value.get("expires") is not None else None,
            http_only=bool(value.get("http_only", False)),
            secure=bool(value.get("secure", False)),
        )


def _matches(cookie: BrowserCookie, url: str, now: datetime) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    domain = cookie.domain.lstrip(".").lower()
    if domain and host != domain and not host.endswith("." + domain):
        return False
    if cookie.secure and parsed.scheme != "https":
        return False
    if cookie.expires is not None and cookie.expires <= now.timestamp():
        return False
    path = parsed.path or "/"
    cookie_path = cookie.path or "/"
    return path == cookie_path or path.startswith(cookie_path.rstrip("/") + "/")


def scoped_cookie_header(
    snapshot: list[BrowserCookie], url: str, *, document_cookie_only: bool = False
) -> str:
    """按请求 URL 生成 Cookie 头;签名视图会排除 HttpOnly Cookie。"""
    now = datetime.now(UTC)
    selected = [
        cookie
        for cookie in snapshot
        if cookie.name and _matches(cookie, url, now)
        and (not document_cookie_only or not cookie.http_only)
    ]
    selected.sort(key=lambda cookie: len(cookie.path or "/"), reverse=True)
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in selected)
