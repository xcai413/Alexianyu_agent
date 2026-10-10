"""Canonical SQLAlchemy ORM model package.

Importing this package registers every table on the shared ``Base.metadata`` while
keeping model definitions split by responsibility.
"""

from .account import Account, Cookie, WsCredential
from .ai import AIProviderConfig
from .audit import AuditLog
from .automation import AlertDispatch, PlatformAutomationConfig, PlatformAutomationRun, TaskLog
from .base import Base
from .chat_assist import BuyerNote, MessageFilterRule, QuickReply
from .conversation import Conversation
from .delivery import (
    ApiDeliveryConfigRow,
    DeliveryAttempt,
    DeliveryGuardRule,
    DeliveryJob,
    DeliveryPolicy,
    DeliveryTemplateRow,
    PersonalBlacklist,
)
from .enums import (
    AccountStatus,
    AuditActor,
    CardType,
    ConsumptionStatus,
    MessageContentType,
    MessageDirection,
    OrderStatus,
    RuleType,
    TaskStatus,
    WorkerCommandStatus,
    WorkerDesiredState,
    WorkerStatusValue,
)
from .idempotency import ConsumerInbox
from .inventory import Card, CardConsumption, InventoryPool, InventoryReservation, InventoryUnit
from .item import (
    BatchPublishEntry,
    BatchPublishJob,
    Item,
    ItemMaterial,
    ItemSpecBinding,
    ItemVariant,
)
from .message import Message
from .notify import NotificationBinding, NotificationChannelRow
from .order import Order
from .outbox import TransactionalOutbox
from .rules import ReplyLog, ReplyRule
from .runtime import DaemonInstance, RuntimeLease, WorkerCommand, WorkerStatus
from .scheduler import SchedulerWork
from .secret import SecretRecord
from .web_session import RevokedWebSession

__all__ = [
    "AIProviderConfig",
    "Account",
    "AccountStatus",
    "AlertDispatch",
    "ApiDeliveryConfigRow",
    "AuditActor",
    "AuditLog",
    "Base",
    "BatchPublishEntry",
    "BatchPublishJob",
    "BuyerNote",
    "Card",
    "CardConsumption",
    "CardType",
    "ConsumerInbox",
    "ConsumptionStatus",
    "Conversation",
    "Cookie",
    "DaemonInstance",
    "DeliveryAttempt",
    "DeliveryGuardRule",
    "DeliveryJob",
    "DeliveryPolicy",
    "DeliveryTemplateRow",
    "InventoryPool",
    "InventoryReservation",
    "InventoryUnit",
    "Item",
    "ItemMaterial",
    "ItemSpecBinding",
    "ItemVariant",
    "Message",
    "MessageContentType",
    "MessageDirection",
    "MessageFilterRule",
    "NotificationBinding",
    "NotificationChannelRow",
    "Order",
    "OrderStatus",
    "PersonalBlacklist",
    "PlatformAutomationConfig",
    "PlatformAutomationRun",
    "QuickReply",
    "ReplyLog",
    "ReplyRule",
    "RevokedWebSession",
    "RuleType",
    "RuntimeLease",
    "SchedulerWork",
    "SecretRecord",
    "TaskLog",
    "TaskStatus",
    "TransactionalOutbox",
    "WorkerCommand",
    "WorkerCommandStatus",
    "WorkerDesiredState",
    "WorkerStatus",
    "WorkerStatusValue",
    "WsCredential",
]
