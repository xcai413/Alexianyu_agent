"""Canonical buyer-conversation persistence model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account
    from .message import Message


class Conversation(Base):
    """One buyer conversation within one seller account."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    buyer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bot_state: Mapped[str] = mapped_column(String(16), default="enabled", nullable=False)
    takeover_state: Mapped[str] = mapped_column(String(16), default="bot", nullable=False)
    context_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    account: Mapped[Account] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(back_populates="conversation")

    __table_args__ = (
        UniqueConstraint(
            "account_id", "external_conversation_id", name="uq_conversations_account_external"
        ),
        Index("ix_conversations_account_last_message", "account_id", "last_message_at"),
        Index("ix_conversations_account_buyer", "account_id", "buyer_id"),
    )
