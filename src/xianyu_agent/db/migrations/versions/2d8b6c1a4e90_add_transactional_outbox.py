"""add transactional outbox

Revision ID: 2d8b6c1a4e90
Revises: 6f3d27b4a8e1
Create Date: 2026-09-08 00:00:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2d8b6c1a4e90"
down_revision: str | None = "6f3d27b4a8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transactional_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=True),
        sa.Column("aggregate_type", sa.String(length=64), nullable=True),
        sa.Column("aggregate_id", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("causation_id", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_transactional_outbox_event_id"),
    )
    with op.batch_alter_table("transactional_outbox", schema=None) as batch_op:
        batch_op.create_index(
            "ix_transactional_outbox_pending",
            ["published_at", "occurred_at", "id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_transactional_outbox_aggregate",
            ["aggregate_type", "aggregate_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_transactional_outbox_account_occurred",
            ["account_id", "occurred_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("transactional_outbox", schema=None) as batch_op:
        batch_op.drop_index("ix_transactional_outbox_account_occurred")
        batch_op.drop_index("ix_transactional_outbox_aggregate")
        batch_op.drop_index("ix_transactional_outbox_pending")
    op.drop_table("transactional_outbox")
