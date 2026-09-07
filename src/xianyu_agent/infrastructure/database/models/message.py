"""Message persistence model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import MessageContentType, MessageDirection

if TYPE_CHECKING:
    from .account import Account


class Message(Base):
    """聊天消息。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    item_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sender_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    direction: Mapped[str] = mapped_column(
        String(16), default=MessageDirection.INBOUND.value, nullable=False
    )
    content_type: Mapped[str] = mapped_column(
        String(32), default=MessageContentType.TEXT.value, nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    account: Mapped[Account] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_messages_account_received", "account_id", "received_at"),
        Index("ix_messages_chat_received", "chat_id", "received_at"),
        UniqueConstraint("account_id", "message_id", name="uq_messages_account_message"),
    )
