"""add platform automation calibration gate and idempotency log

Revision ID: c91a72e4f603
Revises: b7d13c9f4a21
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c91a72e4f603"
down_revision = "b7d13c9f4a21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_automation_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("calibration_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calibration_approved_by", sa.String(length=64), nullable=True),
        sa.Column("schedule_time", sa.String(length=5), nullable=False),
        sa.Column("delay_minutes", sa.Integer(), nullable=False),
        sa.Column("quiet_hours", sa.String(length=11), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "action", name="uq_platform_automation_scope"),
    )
    op.create_index(
        "ix_platform_automation_enabled",
        "platform_automation_configs",
        ["action", "enabled"],
        unique=False,
    )
    op.create_table(
        "platform_automation_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("result", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "action",
            "idempotency_key",
            name="uq_platform_automation_run_idempotency",
        ),
    )
    op.create_index(
        "ix_platform_automation_runs_status",
        "platform_automation_runs",
        ["action", "status", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_platform_automation_runs_status", table_name="platform_automation_runs")
    op.drop_table("platform_automation_runs")
    op.drop_index("ix_platform_automation_enabled", table_name="platform_automation_configs")
    op.drop_table("platform_automation_configs")
