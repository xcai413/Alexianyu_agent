"""add daemon instances

Revision ID: 4dc9fbc82a37
Revises: ee4c2be8f4d1
Create Date: 2026-08-13 00:00:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4dc9fbc82a37"
down_revision: str | None = "ee4c2be8f4d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daemon_instances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("shutdown_requested", sa.Boolean(), nullable=False),
        sa.Column("restart_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("daemon_instances", schema=None) as batch_op:
        batch_op.create_index(
            "ix_daemon_instances_instance_id", ["instance_id"], unique=True
        )
        batch_op.create_index(
            "ix_daemon_status_heartbeat", ["status", "last_heartbeat_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("daemon_instances", schema=None) as batch_op:
        batch_op.drop_index("ix_daemon_status_heartbeat")
        batch_op.drop_index("ix_daemon_instances_instance_id")
    op.drop_table("daemon_instances")
