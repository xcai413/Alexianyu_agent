"""add notification channels and bindings

Revision ID: c3f7a52e9b14
Revises: b5e2c9a71d38
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3f7a52e9b14"
down_revision = "b5e2c9a71d38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("secret_ref", sa.String(length=64), nullable=True),
        sa.Column("events", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_notification_channels_name"),
    )
    op.create_index(
        "ix_notification_channels_enabled", "notification_channels", ["enabled"]
    )

    op.create_table(
        "notification_bindings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"], ["notification_channels.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "account_id", name="uq_notification_bindings"),
    )
    op.create_index(
        "ix_notification_bindings_channel_id", "notification_bindings", ["channel_id"]
    )
    op.create_index(
        "ix_notification_bindings_account_id", "notification_bindings", ["account_id"]
    )

    # 早期只有环境变量里的那个 webhook,所以这一列可空;带上渠道 id 之后,唯一键按渠道区分,
    # 新增一条渠道不会让老渠道重复发送同一条事项。
    with op.batch_alter_table("alert_dispatches") as batch_op:
        batch_op.add_column(sa.Column("channel_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_alert_dispatches_channel_id", ["channel_id"])
        batch_op.create_foreign_key(
            "fk_alert_dispatches_channel_id",
            "notification_channels",
            ["channel_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("alert_dispatches") as batch_op:
        batch_op.drop_constraint("fk_alert_dispatches_channel_id", type_="foreignkey")
        batch_op.drop_index("ix_alert_dispatches_channel_id")
        batch_op.drop_column("channel_id")
    op.drop_index("ix_notification_bindings_account_id", table_name="notification_bindings")
    op.drop_index("ix_notification_bindings_channel_id", table_name="notification_bindings")
    op.drop_table("notification_bindings")
    op.drop_index("ix_notification_channels_enabled", table_name="notification_channels")
    op.drop_table("notification_channels")
