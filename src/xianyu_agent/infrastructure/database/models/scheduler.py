"""Persistence model for business-neutral scheduler work."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

SCHEDULER_DATETIME = DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


class SchedulerWork(Base):
    """Opaque durable work item consumed through the Scheduler Kernel."""

    __tablename__ = "scheduler_work"

    work_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    available_at: Mapped[datetime] = mapped_column(SCHEDULER_DATETIME, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    leased_until: Mapped[datetime | None] = mapped_column(SCHEDULER_DATETIME, nullable=True)

    __table_args__ = (
        Index(
            "ix_scheduler_work_due",
            "available_at",
            "leased_until",
            "work_id",
        ),
        Index("ix_scheduler_work_lease", "lease_owner", "lease_token"),
    )
