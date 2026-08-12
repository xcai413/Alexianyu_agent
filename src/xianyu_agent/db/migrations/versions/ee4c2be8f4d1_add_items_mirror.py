"""add items mirror

Revision ID: ee4c2be8f4d1
Revises: 973e7cb05c16
Create Date: 2026-08-12 00:00:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "ee4c2be8f4d1"
down_revision: str | None = "973e7cb05c16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("price", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("detail_url", sa.String(length=1024), nullable=True),
        sa.Column("main_image_url", sa.String(length=1024), nullable=True),
        sa.Column("category_id", sa.String(length=128), nullable=True),
        sa.Column("auction_type", sa.String(length=64), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("is_on_sale", sa.Boolean(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_items_account_id"), ["account_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_items_item_id"), ["item_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_items_is_on_sale"), ["is_on_sale"], unique=False)
        batch_op.create_index(batch_op.f("ix_items_last_synced_at"), ["last_synced_at"], unique=False)
        batch_op.create_index("uq_items_account_item", ["account_id", "item_id"], unique=True)
        batch_op.create_index("ix_items_account_sale", ["account_id", "is_on_sale"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.drop_index("ix_items_account_sale")
        batch_op.drop_index("uq_items_account_item")
        batch_op.drop_index(batch_op.f("ix_items_last_synced_at"))
        batch_op.drop_index(batch_op.f("ix_items_is_on_sale"))
        batch_op.drop_index(batch_op.f("ix_items_item_id"))
        batch_op.drop_index(batch_op.f("ix_items_account_id"))
    op.drop_table("items")
