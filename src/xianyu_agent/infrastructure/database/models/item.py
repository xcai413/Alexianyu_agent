"""Item mirror persistence model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .account import Account


class Item(Base):
    """账号在售商品的本地只读镜像。"""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    main_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    category_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    auction_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_on_sale: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
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

    account: Mapped[Account] = relationship()

    __table_args__ = (
        Index("uq_items_account_item", "account_id", "item_id", unique=True),
        Index("ix_items_account_sale", "account_id", "is_on_sale"),
    )
