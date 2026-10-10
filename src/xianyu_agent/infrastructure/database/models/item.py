"""Item mirror persistence model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    main_image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    images: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sold_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    # NULL 表示尚未取过详情,与"已确认不是多规格"是两件事。
    has_multi_spec: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 商品级自动化的人工覆盖:NULL 表示由策略判定,exclude 表示完全排除,keep 表示强制保留。
    automation_override: Mapped[str | None] = mapped_column(String(16), nullable=True)
    automation_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    automation_overridden_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    automation_overridden_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    variants: Mapped[list[ItemVariant]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("uq_items_account_item", "account_id", "item_id", unique=True),
        Index("ix_items_account_sale", "account_id", "is_on_sale"),
    )


class ItemVariant(Base):
    """一个商品下的多规格单元(M10)。

    规格以结构化 JSON 辅助存储,业务事实来自 domain 层的 ItemVariant:
    规格名+规格值、单价、库存、图片与启停状态。external_sku_id 允许为空,
    表示运营方自己声明的规格组合,而不是平台返回的 SKU。
    """

    __tablename__ = "item_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_pk: Mapped[int] = mapped_column(
        Integer, ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_sku_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attributes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stock: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
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

    item: Mapped[Item] = relationship(back_populates="variants")

    __table_args__ = (
        UniqueConstraint("item_pk", "external_sku_id", name="uq_item_variants_item_sku"),
        Index("ix_item_variants_item_enabled", "item_pk", "enabled"),
    )


class BatchPublishJob(Base):
    """一次批量铺货(M13)。

    计数列是从条目派生的缓存,由领域方法在同一个事务里一起写入;恢复流程会用条目
    重新对账,因此计数漂移不会成为事实来源。
    """

    __tablename__ = "batch_publish_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    entries: Mapped[list[BatchPublishEntry]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="BatchPublishEntry.row_index"
    )


class ItemSpecBinding(Base):
    """某个规格组合对应哪个库存来源、每次取几份。

    spec_key 是规格组合的规范字符串(由领域层的 binding_key 生成),用它做唯一约束
    比用 JSON 列可靠;specs 保留结构化副本,便于读取时还原领域对象。
    """

    __tablename__ = "item_spec_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_pk: Mapped[int] = mapped_column(
        Integer, ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    spec_key: Mapped[str] = mapped_column(String(512), nullable=False)
    specs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), default="card", nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("item_pk", "spec_key", name="uq_item_spec_bindings_item_spec"),
        Index("ix_item_spec_bindings_item_enabled", "item_pk", "enabled"),
    )


class ItemMaterial(Base):
    """本地图片素材的索引条目(按账号隔离,按内容哈希去重)。"""

    __tablename__ = "item_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="image", nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tags: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("account_id", "content_hash", name="uq_item_materials_account_hash"),
        Index("ix_item_materials_account_enabled", "account_id", "enabled"),
    )


class BatchPublishEntry(Base):
    """批次中的一行草稿及其执行结果。"""

    __tablename__ = "batch_publish_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("batch_publish_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    draft: Mapped[dict] = mapped_column(JSON, nullable=False)
    external_item_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    job: Mapped[BatchPublishJob] = relationship(back_populates="entries")

    __table_args__ = (
        UniqueConstraint("job_id", "row_index", name="uq_batch_publish_entries_job_row"),
        Index("ix_batch_publish_entries_job_state", "job_id", "state"),
    )
