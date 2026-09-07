"""add worker control plane

Revision ID: 9a7e615f2c41
Revises: 4dc9fbc82a37
Create Date: 2026-08-14 00:00:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a7e615f2c41"
down_revision: str | None = "4dc9fbc82a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "desired_state",
                sa.String(length=16),
                server_default="stopped",
                nullable=False,
            )
        )

    accounts = sa.table(
        "accounts",
        sa.column("enabled", sa.Boolean()),
        sa.column("desired_state", sa.String(length=16)),
    )
    op.execute(
        accounts.update()
        .where(accounts.c.enabled.is_(True))
        .values(desired_state="running")
    )

    op.create_table(
        "worker_commands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=32), nullable=False),
        sa.Column("daemon_instance_id", sa.String(length=64), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("worker_commands", schema=None) as batch_op:
        batch_op.create_index("ix_worker_commands_command_id", ["command_id"], unique=True)
        batch_op.create_index("ix_worker_commands_account_id", ["account_id"], unique=False)
        batch_op.create_index("ix_worker_commands_status", ["status"], unique=False)
        batch_op.create_index(
            "ix_worker_commands_pending", ["status", "requested_at"], unique=False
        )
        batch_op.create_index(
            "ix_worker_commands_account_requested",
            ["account_id", "requested_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("worker_commands", schema=None) as batch_op:
        batch_op.drop_index("ix_worker_commands_account_requested")
        batch_op.drop_index("ix_worker_commands_pending")
        batch_op.drop_index("ix_worker_commands_status")
        batch_op.drop_index("ix_worker_commands_account_id")
        batch_op.drop_index("ix_worker_commands_command_id")
    op.drop_table("worker_commands")
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("desired_state")
