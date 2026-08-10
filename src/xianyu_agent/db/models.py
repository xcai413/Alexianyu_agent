"""SQLAlchemy 2.0 ORM 模型 — 11 张表。

四类:
  - 身份类: Account / Cookie / WorkerStatus
  - 业务类: Message / Order / Card / CardConsumption / ReplyRule
  - 日志类: ReplyLog / TaskLog / AuditLog

所有表均带 created_at / updated_at;带外键引用 Account(id) 的列可空(NULL 表示全局)。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """声明基类。"""


# ============================================================================
# 枚举(用于字符串字段,避免 enum 兼容性问题)
# ============================================================================


class AccountStatus(StrEnum):
    """账号运行时状态。"""

    DISABLED = "disabled"  # 用户主动禁用
    OFFLINE = "offline"  # 未启动 Worker
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"  # 持续失败,需手动干预


class WorkerStatus(StrEnum):
    """Worker 实时状态(独立于 Account.status,便于观察心跳)。"""

    ONLINE = "online"
    OFFLINE = "offline"
    RECONNECTING = "reconnecting"
    ERROR = "error"


class MessageDirection(StrEnum):
    """消息方向。"""

    INBOUND = "inbound"  # 买家 -> 我
    OUTBOUND = "outbound"  # 我 -> 买家


class MessageContentType(StrEnum):
    """消息内容类型。"""

    TEXT = "text"
    IMAGE = "image"
    CARD = "card"  # 卡密卡片
    SYSTEM = "system"  # 系统通知
    PRODUCT = "product"  # 商品卡片


class OrderStatus(StrEnum):
    """订单状态。"""

    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class CardType(StrEnum):
    """卡密四型(借鉴 GoofishEngine 领域模型)。"""

    TEXT = "text"  # 纯文本卡密(一行一个)
    DATA = "data"  # 结构化 JSON 数据
    IMAGE = "image"  # 图片(发货时附带)
    API = "api"  # 调用外部 API 获取


class ConsumptionStatus(StrEnum):
    """卡密消费状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    RETRY = "retry"
    PENDING = "pending"


class RuleType(StrEnum):
    """回复规则类型。"""

    KEYWORD = "keyword"  # 关键词包含匹配(大小写不敏感)
    REGEX = "regex"  # 正则匹配
    DEFAULT = "default"  # 默认回复(无其他规则命中时)


class TaskStatus(StrEnum):
    """定时任务执行状态。"""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class AuditActor(StrEnum):
    """审计日志的触发来源。"""

    CLI = "cli"
    MCP = "mcp"
    SKILL = "skill"
    TUI = "tui"
    SYSTEM = "system"  # 定时任务 / 内部调度


# ============================================================================
# 模型
# ============================================================================


class Account(Base):
    """闲鱼账号基本信息。"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    nickname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=AccountStatus.OFFLINE, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    cookies: Mapped[list[Cookie]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    messages: Mapped[list[Message]] = relationship(back_populates="account")
    orders: Mapped[list[Order]] = relationship(back_populates="account")
    cards: Mapped[list[Card]] = relationship(back_populates="account")
    rules: Mapped[list[ReplyRule]] = relationship(back_populates="account")
    worker_status: Mapped[WorkerStatus | None] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (Index("ix_accounts_enabled_status", "enabled", "status"),)

    def __repr__(self) -> str:
        return f"<Account {self.account_id} status={self.status}>"


class Cookie(Base):
    """账号 Cookie(Fernet 加密存储)。"""

    __tablename__ = "cookies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="cookies")

    def __repr__(self) -> str:
        return f"<Cookie account_id={self.account_id}>"


class WorkerStatus(Base):
    """单账号 Worker 心跳(独立于 Account 便于轮询)。"""

    __tablename__ = "worker_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default=WorkerStatus.OFFLINE, nullable=False)
    reconnect_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="worker_status")


class Message(Base):
    """聊天消息。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    sender_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    direction: Mapped[str] = mapped_column(
        String(16), default=MessageDirection.INBOUND, nullable=False
    )
    content_type: Mapped[str] = mapped_column(
        String(32), default=MessageContentType.TEXT, nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    account: Mapped[Account] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_messages_account_received", "account_id", "received_at"),
        Index("ix_messages_chat_received", "chat_id", "received_at"),
    )


class Order(Base):
    """订单。"""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    item_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    item_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    buyer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    buyer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=OrderStatus.PENDING_PAYMENT, nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="orders")
    consumptions: Mapped[list[CardConsumption]] = relationship(back_populates="order")

    __table_args__ = (
        Index("uq_orders_account_order", "account_id", "order_id", unique=True),
        Index("ix_orders_status_paid", "status", "paid_at"),
    )


class Card(Base):
    """卡密库存。"""

    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), default=CardType.TEXT, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)  # 一行一个卡密
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    remaining: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="cards")
    consumptions: Mapped[list[CardConsumption]] = relationship(
        back_populates="card", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_cards_account_enabled", "account_id", "enabled"),)


class CardConsumption(Base):
    """卡密消费记录(原子扣减审计追踪)。"""

    __tablename__ = "card_consumptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=ConsumptionStatus.SUCCESS, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    card: Mapped[Card] = relationship(back_populates="consumptions")
    order: Mapped[Order | None] = relationship(back_populates="consumptions")

    __table_args__ = (Index("ix_consumptions_consumed_at", "consumed_at"),)


class ReplyRule(Base):
    """回复规则。"""

    __tablename__ = "reply_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )  # NULL = 全局规则
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), default=RuleType.KEYWORD, nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    reply_text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[Account | None] = relationship(back_populates="rules")
    reply_logs: Mapped[list[ReplyLog]] = relationship(back_populates="rule")

    __table_args__ = (
        Index("ix_rules_account_enabled_priority", "account_id", "enabled", "priority"),
    )


class ReplyLog(Base):
    """回复发送日志。"""

    __tablename__ = "reply_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    rule_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reply_rules.id", ondelete="SET NULL"), nullable=True
    )
    sent_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), default="rule", nullable=False
    )  # rule / ai / manual
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped[Account] = relationship()
    message: Mapped[Message | None] = relationship(foreign_keys=[message_id])
    rule: Mapped[ReplyRule | None] = relationship(back_populates="reply_logs")


class TaskLog(Base):
    """定时任务执行日志。"""

    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.RUNNING, nullable=False)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_tasks_started_at", "started_at"),)


class AuditLog(Base):
    """审计日志(谁在什么时候做了什么)。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(
        String(16), default=AuditActor.SYSTEM, nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_audit_actor_created", "actor", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
    )
