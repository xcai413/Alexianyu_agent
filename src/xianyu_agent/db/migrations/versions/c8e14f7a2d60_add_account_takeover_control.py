"""add account takeover control

Revision ID: c8e14f7a2d60
Revises: d9c4a7e15b32
Create Date: 2026-09-20 04:30:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e14f7a2d60"
down_revision: str | None = "d9c4a7e15b32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """持久化接管批准与连续失败计数,供所有副本共享。"""
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("takeover_approved_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("takeover_approved_by", sa.String(length=64)))
        batch_op.add_column(
            sa.Column(
                "takeover_failure_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("takeover_last_failure_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("takeover_last_failure_at")
        batch_op.drop_column("takeover_failure_count")
        batch_op.drop_column("takeover_approved_by")
        batch_op.drop_column("takeover_approved_at")
