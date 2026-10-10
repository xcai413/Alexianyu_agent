"""发货策略与发货任务持久化模型(Issue #5 Phase 6)。"""

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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

if TYPE_CHECKING:
    from .inventory import InventoryPool


class DeliveryPolicy(Base):
    """发货策略:范围字段决定层级(规格 > 商品 > 账号 > 全局)。"""

    __tablename__ = "delivery_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    inventory_pool_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("inventory_pools.id"), nullable=False, index=True
    )
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sku_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivery_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    template: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 可选的模板库引用:与内联 template 二选一,内联优先。
    template_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    platform_confirm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    pool: Mapped[InventoryPool] = relationship()

    __table_args__ = (
        Index("ix_delivery_policies_scope", "account_id", "item_id"),
        Index("ix_delivery_policies_enabled", "enabled"),
    )


class DeliveryJob(Base):
    """一个订单的一次履约任务;唯一键保证重复事件收敛到同一条记录。"""

    __tablename__ = "delivery_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    semantic_key: Mapped[str] = mapped_column(String(64), nullable=False, default="order_paid")
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    required_quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    policy_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    reservation_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("inventory_reservations.id", ondelete="SET NULL"),
        nullable=True,
    )
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sku_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    attempts: Mapped[list[DeliveryAttempt]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id", "order_id", "semantic_key", name="uq_delivery_jobs_identity"
        ),
        Index("ix_delivery_jobs_state_created", "state", "created_at"),
    )


class DeliveryAttempt(Base):
    """一次外部动作的尝试记录,append-only。"""

    __tablename__ = "delivery_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("delivery_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[DeliveryJob] = relationship(back_populates="attempts")

    __table_args__ = (UniqueConstraint("job_id", "attempt_no", name="uq_delivery_attempts_no"),)


class DeliveryGuardRule(Base):
    """发货守卫规则:账号级或全局(account_id 为空)。"""

    __tablename__ = "delivery_guard_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rule_code: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    block_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    excluded_item_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    config: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
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
        # 注意:SQLite/PostgreSQL 认为 NULL 互不相等,全局规则(account_id 为空)的唯一性
        # 由应用层 upsert 保证,这里只约束有账号的规则。
        UniqueConstraint("account_id", "rule_code", name="uq_delivery_guard_rules_scope"),
    )


class PersonalBlacklist(Base):
    """买家黑名单:owner 全局、账号级或商品级。"""

    __tablename__ = "personal_blacklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    buyer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_personal_blacklist_lookup", "buyer_id", "account_id"),)


class ApiDeliveryConfigRow(Base):
    """外部发卡 API 的结构化配置(密钥只存引用,不存明文)。"""

    __tablename__ = "api_delivery_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False, default="POST")
    headers: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    response_path: Mapped[str] = mapped_column(String(128), nullable=False, default="card")
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (UniqueConstraint("name", name="uq_api_delivery_configs_name"),)


class DeliveryTemplateRow(Base):
    """可复用发货模板库(参考 Helper 的发货模板)。

    模板只存文案:变量白名单校验在 domain 层完成,这里不做业务判断。
    """

    __tablename__ = "delivery_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
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

    __table_args__ = (UniqueConstraint("name", name="uq_delivery_templates_name"),)
