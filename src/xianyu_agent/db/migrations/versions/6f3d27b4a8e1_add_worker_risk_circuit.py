"""add worker risk circuit

Revision ID: 6f3d27b4a8e1
Revises: f1a55ff834d2
Create Date: 2026-08-21 00:00:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

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
    # 将已有的明确人工验证错误纳入新熔断模型；不尝试请求 Token，也不会改 Cookie。
    op.execute(
        """
        UPDATE worker_status
        SET risk_code = 'FAIL_SYS_USER_VALIDATE',
            risk_detected_at = COALESCE(updated_at, CURRENT_TIMESTAMP),
            risk_cooldown_until = datetime(COALESCE(updated_at, CURRENT_TIMESTAMP), '+1200 seconds'),
            risk_recovery_required = 1,
            status = 'risk_cooling',
            last_error = 'FAIL_SYS_USER_VALIDATE'
        WHERE risk_code IS NULL
          AND last_error LIKE '%FAIL_SYS_USER_VALIDATE%'
        """
    )
    op.execute(
        """
        UPDATE accounts
        SET desired_state = 'stopped'
        WHERE id IN (
            SELECT account_id
            FROM worker_status
            WHERE risk_recovery_required = 1
        )
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("worker_status", schema=None) as batch_op:
        batch_op.drop_column("risk_recovery_required")
        batch_op.drop_column("risk_cooldown_until")
        batch_op.drop_column("risk_detected_at")
        batch_op.drop_column("risk_code")
