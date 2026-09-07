"""Account and credential persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import AccountStatus, WorkerDesiredState

if TYPE_CHECKING:
    from .inventory import Card
    from .message import Message
    from .order import Order
    from .rules import ReplyRule
    from .runtime import WorkerCommand, WorkerStatus


class Account(Base):
    """闲鱼账号基本信息。"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    nickname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    desired_state: Mapped[str] = mapped_column(
        String(16), default=WorkerDesiredState.STOPPED.value, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), default=AccountStatus.OFFLINE.value, nullable=False
    )
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
    ws_credential: Mapped[WsCredential | None] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False
    )
    messages: Mapped[list[Message]] = relationship(back_populates="account")
    orders: Mapped[list[Order]] = relationship(back_populates="account")
    cards: Mapped[list[Card]] = relationship(back_populates="account")
    rules: Mapped[list[ReplyRule]] = relationship(back_populates="account")
    worker_status: Mapped[WorkerStatus | None] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False
    )
    worker_commands: Mapped[list[WorkerCommand]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
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


class WsCredential(Base):
    """闲鱼 IM Access Token 与稳定设备 ID;Token 仅保存 Fernet 密文。"""

    __tablename__ = "ws_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped[Account] = relationship(back_populates="ws_credential")
