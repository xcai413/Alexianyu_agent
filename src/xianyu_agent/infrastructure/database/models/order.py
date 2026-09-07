"""Order mirror persistence model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import OrderStatus

if TYPE_CHECKING:
    from .account import Account
    from .inventory import CardConsumption


class Order(Base):
    """订单本地镜像。"""

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
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=OrderStatus.PENDING_PAYMENT.value, nullable=False
    )
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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

    account: Mapped["Account"] = relationship(back_populates="orders")
    consumptions: Mapped[list["CardConsumption"]] = relationship(back_populates="order")

    __table_args__ = (
        Index("uq_orders_account_order", "account_id", "order_id", unique=True),
        Index("ix_orders_status_paid", "status", "paid_at"),
        Index("ix_orders_account_item_status", "account_id", "item_id", "status"),
    )
