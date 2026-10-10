"""Card inventory persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
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


class InventoryPool(Base):
    """库存池:一处发货来源的定义(text/data/image/api)。

    只有 data 池持有可消费单元;api 池只保存 api_config_ref(配置记录或机密库条目的引用),
    密钥本身不进这张表。
    """

    __tablename__ = "inventory_pools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    account_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sku_scope: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_config_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    units: Mapped[list[InventoryUnit]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )
    reservations: Mapped[list[InventoryReservation]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_inventory_pools_scope", "account_scope", "item_scope"),)


class InventoryReservation(Base):
    """一次预留:一个发货 job 从某个池占住 N 条单元。"""

    __tablename__ = "inventory_reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inventory_pools.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    pool: Mapped[InventoryPool] = relationship(back_populates="reservations")

    __table_args__ = (
        Index("ix_inventory_reservations_pool_job", "pool_id", "job_ref"),
        Index("ix_inventory_reservations_state", "state"),
    )


class InventoryUnit(Base):
    """data 池里的一条卡密单元:状态机决定它归谁。"""

    __tablename__ = "inventory_units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pool_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inventory_pools.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reservation_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("inventory_reservations.id", ondelete="SET NULL"),
        nullable=True,
    )
    reserved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    unit_metadata: Mapped[dict[str, object] | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    pool: Mapped[InventoryPool] = relationship(back_populates="units")

    __table_args__ = (
        Index("ix_inventory_units_pool_state", "pool_id", "state"),
        Index("ix_inventory_units_reservation", "reservation_id"),
    )
