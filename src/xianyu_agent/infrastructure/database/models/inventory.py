"""Card inventory persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .enums import CardType, ConsumptionStatus

if TYPE_CHECKING:
    from .account import Account
    from .order import Order


class Card(Base):
    """卡密库存。"""

    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), default=CardType.TEXT.value, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        String(16), default=ConsumptionStatus.SUCCESS.value, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    card: Mapped[Card] = relationship(back_populates="consumptions")
    order: Mapped[Order | None] = relationship(back_populates="consumptions")

    __table_args__ = (Index("ix_consumptions_consumed_at", "consumed_at"),)
