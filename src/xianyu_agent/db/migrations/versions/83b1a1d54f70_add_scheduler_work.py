"""add durable scheduler work

Revision ID: 83b1a1d54f70
Revises: 2d8b6c1a4e90
Create Date: 2026-09-08 03:15:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "83b1a1d54f70"
down_revision: str | None = "2d8b6c1a4e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEDULER_DATETIME = sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "scheduler_work",
        sa.Column("work_id", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("available_at", SCHEDULER_DATETIME, nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("leased_until", SCHEDULER_DATETIME, nullable=True),
        sa.PrimaryKeyConstraint("work_id"),
    )
    with op.batch_alter_table("scheduler_work", schema=None) as batch_op:
        batch_op.create_index(
            "ix_scheduler_work_due",
            ["available_at", "leased_until", "work_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_scheduler_work_lease",
            ["lease_owner", "lease_token"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduler_work", schema=None) as batch_op:
        batch_op.drop_index("ix_scheduler_work_lease")
        batch_op.drop_index("ix_scheduler_work_due")
    op.drop_table("scheduler_work")
