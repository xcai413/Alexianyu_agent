"""把一条待人工处理事项渲染成各渠道的请求(纯函数,不发请求)。

载荷形状按各家渠道公开的机器人协议构造:钉钉与飞书的加签算法在各自的文档里是固定的,
`now` 显式注入是为了让加签可以被测试。这里不读环境、不发网络请求,只是把"要发什么"算出来。

正文只由事项自身的字段拼成(类型、账号、目标、状态、原因、收尾命令),这些字段里本来就不含
Cookie / Token / 卡密,所以通知不会成为新的泄密面。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

from .channel import ChannelError, NotificationChannel, event_label

TELEGRAM_API = "https://api.telegram.org"


@dataclass(frozen=True)
class NotifyMessage:
    """一条要发出去的通知:标题、正文与用于过滤的事件字段。"""

    event: str
    title: str
    body: str
    level: str = "warning"
    account_id: str | None = None
    target: str = ""
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class NotifyRequest:
    """一次渠道出站的完整描述:方法、地址、请求体与附加请求头。"""

    method: str
    url: str
    payload: dict[str, object]
    headers: dict[str, str] = field(default_factory=dict)


def render_message(message: NotifyMessage) -> str:
    """纯文本正文:每个渠道都能原样带过去,不引第二套模板引擎。"""
    lines = [f"【{message.title}】"]
    if message.account_id:
        lines.append(f"账号:{message.account_id}")
    lines.append(f"事件:{event_label(message.event)}")
    if message.target:
        lines.append(f"对象:{message.target}")
    lines.append(f"状态:{message.level}")
    if message.body:
        lines.append(message.body)
    return "\n".join(lines)


def _signed_query(url: str, *, secret: str, now: datetime) -> str:
    """钉钉机器人加签:把 timestamp 与 sign 追加到查询串上。"""
    timestamp = str(int(now.timestamp() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256)
    sign = base64.b64encode(digest.digest()).decode("utf-8")
    parts = urlsplit(url)
    extra = urlencode({"timestamp": timestamp, "sign": sign})
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def feishu_sign(*, secret: str, now: datetime) -> dict[str, str]:
    """飞书自定义机器人加签:密钥是 `timestamp\\nsecret`,消息体为空。"""
    timestamp = str(int(now.timestamp()))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256)
    return {"timestamp": timestamp, "sign": base64.b64encode(digest.digest()).decode("utf-8")}


def build_request(
    channel: NotificationChannel,
    message: NotifyMessage,
    *,
    secret: str | None = None,
    now: datetime | None = None,
) -> NotifyRequest:
    """把渠道与消息组合成一次出站请求。密钥由调用方从 SecretVault 取出后传进来。"""
    moment = now or datetime.now(UTC)
    text = render_message(message)
    if channel.kind == "webhook":
        return NotifyRequest(
            method="POST",
            url=channel.target,
            payload={
                "event": message.event,
                "title": message.title,
                "body": message.body,
                "text": text,
                "level": message.level,
                "account_id": message.account_id,
                "target": message.target,
                "occurred_at": (
                    message.occurred_at.isoformat() if message.occurred_at is not None else None
                ),
            },
            headers={"Content-Type": "application/json"},
        )
    if channel.kind == "dingtalk":
        url = channel.target
        if secret:
            url = _signed_query(url, secret=secret, now=moment)
        return NotifyRequest(
            method="POST",
            url=url,
            payload={
                "msgtype": "markdown",
                "markdown": {"title": message.title, "text": text},
            },
            headers={"Content-Type": "application/json"},
        )
    if channel.kind == "feishu":
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if secret:
            payload.update(feishu_sign(secret=secret, now=moment))
        return NotifyRequest(
            method="POST",
            url=channel.target,
            payload=payload,
            headers={"Content-Type": "application/json"},
        )
    if channel.kind == "bark":
        if not secret:
            msg = "bark 渠道缺少设备密钥"
            raise ChannelError(msg)
        base = channel.target.rstrip("/")
        return NotifyRequest(
            method="POST",
            url=f"{base}/push",
            payload={
                "device_key": secret,
                "title": message.title,
                "body": text,
                "group": "xianyu",
            },
            headers={"Content-Type": "application/json"},
        )
    if channel.kind == "telegram":
        if not secret:
            msg = "telegram 渠道缺少机器人 token"
            raise ChannelError(msg)
        return NotifyRequest(
            method="POST",
            url=f"{TELEGRAM_API}/bot{secret}/sendMessage",
            payload={
                "chat_id": channel.target,
                "text": text,
                "disable_web_page_preview": True,
            },
            headers={"Content-Type": "application/json"},
        )
    msg = f"不支持的通知渠道类型 {channel.kind!r}"
    raise ChannelError(msg)
