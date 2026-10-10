"""add account scoped message filter rules

Revision ID: 9c2e7f1a4b60
Revises: f4b8e26a9c17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c2e7f1a4b60"
down_revision = "f4b8e26a9c17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_filter_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("keyword", sa.String(length=255), nullable=False),
        sa.Column("filter_type", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "keyword", "filter_type", name="uq_message_filter_rule_scope"),
    )
    op.create_index("ix_message_filter_rules_account_id", "message_filter_rules", ["account_id"])
    op.create_index("ix_message_filter_rules_enabled", "message_filter_rules", ["account_id", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_message_filter_rules_enabled", table_name="message_filter_rules")
    op.drop_index("ix_message_filter_rules_account_id", table_name="message_filter_rules")
    op.drop_table("message_filter_rules")
