"""String-valued persistence enums used by ORM defaults and validation."""

from enum import StrEnum


class AccountStatus(StrEnum):
    DISABLED = "disabled"
    OFFLINE = "offline"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class WorkerStatusValue(StrEnum):
    """Persisted worker heartbeat status value.

    The old monolithic module used ``WorkerStatus`` for both this enum and the ORM
    model. The split removes that name collision while preserving stored values.
    """

    ONLINE = "online"
    OFFLINE = "offline"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class WorkerDesiredState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"


class WorkerCommandStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    CARD = "card"
    SYSTEM = "system"
    PRODUCT = "product"


class OrderStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class CardType(StrEnum):
    TEXT = "text"
    DATA = "data"
    IMAGE = "image"
    API = "api"


class ConsumptionStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    RETRY = "retry"
    PENDING = "pending"


class RuleType(StrEnum):
    KEYWORD = "keyword"
    REGEX = "regex"
    DEFAULT = "default"


class TaskStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class AuditActor(StrEnum):
    CLI = "cli"
    MCP = "mcp"
    SKILL = "skill"
    TUI = "tui"
    SYSTEM = "system"
