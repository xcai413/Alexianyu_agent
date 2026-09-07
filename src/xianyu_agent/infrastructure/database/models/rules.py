"""Reply rule and reply log persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import RuleType

if TYPE_CHECKING:
    from .account import Account
    from .message import Message


class ReplyRule(Base):
    """回复规则。"""

    __tablename__ = "reply_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), default=RuleType.KEYWORD.value, nullable=False)
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
    source: Mapped[str] = mapped_column(String(16), default="rule", nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped[Account] = relationship()
    message: Mapped[Message | None] = relationship(foreign_keys=[message_id])
    rule: Mapped[ReplyRule | None] = relationship(back_populates="reply_logs")
