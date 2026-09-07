"""Audit persistence model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import AuditActor


class AuditLog(Base):
    """审计日志(谁在什么时候做了什么)。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(
        String(16), default=AuditActor.SYSTEM.value, nullable=False, index=True
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
