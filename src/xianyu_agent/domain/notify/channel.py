"""通知渠道的领域模型(对照 Helper 的 `/notifications/channels`)。

渠道回答的是"把运营通知推到哪儿"。它不碰闲鱼平台,也不会产生任何买家可见的动作,因此不需要
平台校准门禁;但两条纪律与原仓库一致:

1. **只允许 https**:地址可能是运营手填的,所以每次出站前还要再过一遍出站守卫。
2. **密钥不进配置**:机器人 token / 签名 secret 在库里只以 SecretVault 的引用名出现,任何
   列表、日志与接口响应都只回掩码。

事件词表直接沿用待办清单的 `kind`,不另造一套分类,避免同一件事在两个界面里叫两个名字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

CHANNEL_KINDS: tuple[str, ...] = ("webhook", "dingtalk", "feishu", "bark", "telegram")

#: 与 `AttentionItem.kind` 同词表;空列表表示订阅全部事件。
NOTIFY_EVENTS: tuple[str, ...] = (
    "delivery_job",
    "automation_run",
    "account_risk",
    "message_received",
)

EVENT_LABELS: dict[str, str] = {
    "delivery_job": "发货任务",
    "automation_run": "平台自动化",
    "account_risk": "账号风控",
    "message_received": "收到买家消息",
}

MAX_CHANNEL_NAME_LENGTH = 128

#: 这些渠道类型的 target 不是 URL,而是对方的会话标识。
NON_URL_TARGET_KINDS: tuple[str, ...] = ("telegram",)

#: 这些渠道类型必须有 SecretVault 引用,否则连不上。
SECRET_REQUIRED_KINDS: tuple[str, ...] = ("telegram",)


class ChannelError(ValueError):
    """通知渠道配置不合法。"""


def event_label(event: str) -> str:
    """事件的中文名;未知事件原样返回,不隐藏。"""
    return EVENT_LABELS.get(event, event)


def _check_events(events: tuple[str, ...]) -> tuple[str, ...]:
    unknown = [event for event in events if event not in NOTIFY_EVENTS]
    if unknown:
        msg = f"未知的通知事件 {'、'.join(unknown)},只支持 {'、'.join(NOTIFY_EVENTS)}"
        raise ChannelError(msg)
    # 去重但保留书写顺序:同一事件写两遍没有意义,顺序则影响界面展示。
    seen: dict[str, None] = {}
    for event in events:
        seen.setdefault(event, None)
    return tuple(seen)


@dataclass(frozen=True)
class NotificationChannel:
    """一条通知渠道:推到哪个地址、订阅哪些事件、用哪个密钥。"""

    name: str
    kind: str
    target: str
    secret_ref: str | None = None
    events: tuple[str, ...] = field(default=())
    enabled: bool = True
    description: str | None = None
    local_id: int | None = None

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            msg = "渠道名称不能为空"
            raise ChannelError(msg)
        if len(name) > MAX_CHANNEL_NAME_LENGTH:
            msg = f"渠道名称不能超过 {MAX_CHANNEL_NAME_LENGTH} 个字符"
            raise ChannelError(msg)
        if self.kind not in CHANNEL_KINDS:
            msg = f"不支持的通知渠道类型 {self.kind!r},只支持 {'、'.join(CHANNEL_KINDS)}"
            raise ChannelError(msg)
        target = self.target.strip()
        if not target:
            msg = "通知目标不能为空"
            raise ChannelError(msg)
        if self.kind not in NON_URL_TARGET_KINDS:
            parts = urlsplit(target)
            if parts.scheme != "https":
                msg = f"通知目标只允许 https,当前是 {parts.scheme or '空'}"
                raise ChannelError(msg)
            if not parts.hostname:
                msg = "通知目标缺少主机名"
                raise ChannelError(msg)
        if self.local_id is not None and self.local_id <= 0:
            msg = "渠道 id 必须为正整数"
            raise ChannelError(msg)
        secret_ref = (self.secret_ref or "").strip() or None
        if self.kind in SECRET_REQUIRED_KINDS and secret_ref is None:
            msg = f"{self.kind} 渠道必须提供 SecretVault 引用"
            raise ChannelError(msg)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "secret_ref", secret_ref)
        object.__setattr__(self, "events", _check_events(tuple(self.events)))
        if self.description is not None:
            object.__setattr__(self, "description", self.description.strip() or None)

    def accepts(self, event: str) -> bool:
        """空事件列表表示订阅全部;否则只收列出来的事件。"""
        return not self.events or event in self.events

    def masked_target(self) -> str:
        """可以进日志与界面的目标预览:URL 只留主机名,路径可能含 token,一律打掉。"""
        if self.kind in NON_URL_TARGET_KINDS:
            head = self.target[:4]
            return f"{head}…"
        parts = urlsplit(self.target)
        return f"{parts.scheme}://{parts.hostname}/…"

    def describe(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        events = "、".join(event_label(event) for event in self.events) or "全部事件"
        return f"{self.name}(#{self.local_id or '-'} {self.kind} {state} {events})"
