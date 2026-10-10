"""通知渠道领域模型与载荷构造(纯逻辑,无 IO)。"""

from .channel import (
    CHANNEL_KINDS,
    NOTIFY_EVENTS,
    ChannelError,
    NotificationChannel,
    event_label,
)
from .message import NotifyMessage, NotifyRequest, build_request, render_message

__all__ = [
    "CHANNEL_KINDS",
    "NOTIFY_EVENTS",
    "ChannelError",
    "NotificationChannel",
    "NotifyMessage",
    "NotifyRequest",
    "build_request",
    "event_label",
    "render_message",
]
