"""add alert dispatch log

Revision ID: b5e2c9a71d38
Revises: a1c7f3d84b26
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b5e2c9a71d38"
down_revision = "a1c7f3d84b26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_dispatches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_alert_dispatches_key"),
    )
    op.create_index("ix_alert_dispatches_created", "alert_dispatches", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_alert_dispatches_created", table_name="alert_dispatches")
    op.drop_table("alert_dispatches")
