"""add quick replies and buyer notes

Revision ID: f4b8e26a9c17
Revises: e7c3d10a5b62
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4b8e26a9c17"
down_revision = "e7c3d10a5b62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quick_replies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "title", name="uq_quick_replies_scope_title"),
    )
    op.create_index("ix_quick_replies_account_id", "quick_replies", ["account_id"])
    op.create_index(
        "ix_quick_replies_sort", "quick_replies", ["account_id", "sort_order"]
    )
    op.create_table(
        "buyer_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("buyer_id", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
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
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "buyer_id", name="uq_buyer_notes_scope"),
    )
    op.create_index("ix_buyer_notes_account_id", "buyer_notes", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_buyer_notes_account_id", table_name="buyer_notes")
    op.drop_table("buyer_notes")
    op.drop_index("ix_quick_replies_sort", table_name="quick_replies")
    op.drop_index("ix_quick_replies_account_id", table_name="quick_replies")
    op.drop_table("quick_replies")
