"""Canonical SQLAlchemy ORM model package.

Importing this package registers every table on the shared ``Base.metadata`` while
keeping model definitions split by responsibility.
"""

from .account import Account, Cookie, WsCredential
from .audit import AuditLog
from .automation import TaskLog
from .base import Base
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
from .inventory import Card, CardConsumption
from .item import Item
from .message import Message
from .order import Order
from .outbox import TransactionalOutbox
from .rules import ReplyLog, ReplyRule
from .runtime import DaemonInstance, WorkerCommand, WorkerStatus

__all__ = [
    "Account",
    "AccountStatus",
    "AuditActor",
    "AuditLog",
    "Base",
    "Card",
    "CardConsumption",
    "CardType",
    "ConsumptionStatus",
    "Cookie",
    "DaemonInstance",
    "Item",
    "Message",
    "MessageContentType",
    "MessageDirection",
    "Order",
    "OrderStatus",
    "ReplyLog",
    "ReplyRule",
    "RuleType",
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
