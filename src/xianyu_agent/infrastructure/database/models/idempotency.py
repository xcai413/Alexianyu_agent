"""Durable consumer inbox used for idempotent event handling."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ConsumerInbox(Base):
    """One committed idempotency reservation per consumer namespace and key hash."""

    __tablename__ = "consumer_inbox"

    consumer: Mapped[str] = mapped_column(String(128), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
