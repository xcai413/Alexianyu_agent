"""add worker risk circuit

Revision ID: 6f3d27b4a8e1
Revises: f1a55ff834d2
Create Date: 2026-08-21 00:00:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision: str = "6f3d27b4a8e1"
down_revision: str | None = "f1a55ff834d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("worker_status", schema=None) as batch_op:
        batch_op.add_column(sa.Column("risk_code", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("risk_detected_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("risk_cooldown_until", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "risk_recovery_required",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    # 将已有的明确人工验证错误纳入新熔断模型;不尝试请求 Token,也不会改 Cookie。
    # 使用 SQLAlchemy 表达式和 Python timedelta,避免 SQLite datetime() / integer boolean
    # 这类方言专属 SQL 进入历史迁移。
    worker_status = sa.table(
        "worker_status",
        sa.column("id", sa.Integer()),
        sa.column("account_id", sa.Integer()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("last_error", sa.Text()),
        sa.column("risk_code", sa.String(length=64)),
        sa.column("risk_detected_at", sa.DateTime(timezone=True)),
        sa.column("risk_cooldown_until", sa.DateTime(timezone=True)),
        sa.column("risk_recovery_required", sa.Boolean()),
        sa.column("status", sa.String(length=32)),
    )
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Integer()),
        sa.column("desired_state", sa.String(length=16)),
    )

    bind = op.get_bind()
    current_timestamp = bind.scalar(sa.select(sa.func.current_timestamp()))
    assert isinstance(current_timestamp, datetime)

    validation_rows = bind.execute(
        sa.select(worker_status.c.id, worker_status.c.updated_at).where(
            worker_status.c.risk_code.is_(None),
            worker_status.c.last_error.like("%FAIL_SYS_USER_VALIDATE%"),
        )
    ).all()
    for row in validation_rows:
        detected_at = row.updated_at or current_timestamp
        bind.execute(
            worker_status.update()
            .where(worker_status.c.id == row.id)
            .values(
                risk_code="FAIL_SYS_USER_VALIDATE",
                risk_detected_at=detected_at,
                risk_cooldown_until=detected_at + timedelta(seconds=1200),
                risk_recovery_required=True,
                status="risk_cooling",
                last_error="FAIL_SYS_USER_VALIDATE",
            )
        )

    affected_accounts = sa.select(worker_status.c.account_id).where(
        worker_status.c.risk_recovery_required.is_(True)
    )
    op.execute(
        accounts.update()
        .where(accounts.c.id.in_(affected_accounts))
        .values(desired_state="stopped")
    )


def downgrade() -> None:
    with op.batch_alter_table("worker_status", schema=None) as batch_op:
        batch_op.drop_column("risk_recovery_required")
        batch_op.drop_column("risk_cooldown_until")
        batch_op.drop_column("risk_detected_at")
        batch_op.drop_column("risk_code")
