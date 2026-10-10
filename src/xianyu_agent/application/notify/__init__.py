"""多渠道通知:把待人工处理事项推到运营自己配置的渠道。"""

from .channel_service import NotificationChannelService
from .dispatcher import DispatchResult, NotificationDispatcher
from .sender import ChannelSender, SendResult

__all__ = [
    "ChannelSender",
    "DispatchResult",
    "NotificationChannelService",
    "NotificationDispatcher",
    "SendResult",
]
