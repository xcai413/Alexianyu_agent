"""Runtime control-plane persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import WorkerCommandStatus, WorkerStatusValue

if TYPE_CHECKING:
    from .account import Account


class WorkerStatus(Base):
    """单账号 Worker 心跳(独立于 Account 便于轮询)。"""

    __tablename__ = "worker_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=WorkerStatusValue.OFFLINE.value, nullable=False
    )
    reconnect_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    risk_cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    risk_recovery_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="worker_status")


class DaemonInstance(Base):
    """常驻 daemon 的进程级状态与控制标记。"""

    __tablename__ = "daemon_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="starting", nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    shutdown_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    restart_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_daemon_status_heartbeat", "status", "last_heartbeat_at"),
    )


class WorkerCommand(Base):
    """CLI/MCP 提交、daemon 执行的账号级持久化命令。"""

    __tablename__ = "worker_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=WorkerCommandStatus.PENDING.value, nullable=False, index=True
    )
    requested_by: Mapped[str] = mapped_column(String(32), default="cli", nullable=False)
    daemon_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="worker_commands")

    __table_args__ = (
        Index("ix_worker_commands_pending", "status", "requested_at"),
        Index("ix_worker_commands_account_requested", "account_id", "requested_at"),
    )
