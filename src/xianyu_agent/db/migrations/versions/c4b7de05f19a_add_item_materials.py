"""add item materials

Revision ID: c4b7de05f19a
Revises: a8d1f5c70e42
Create Date: 2026-09-19 15:05:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c4b7de05f19a"
down_revision: str | None = "a8d1f5c70e42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Index local image assets per account with content-hash de-duplication."""
    op.create_table(
        "item_materials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=80), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "content_hash", name="uq_item_materials_account_hash"
        ),
    )
    with op.batch_alter_table("item_materials", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_item_materials_account_id"), ["account_id"], unique=False
        )
        batch_op.create_index(
            "ix_item_materials_account_enabled", ["account_id", "enabled"], unique=False
        )


def downgrade() -> None:
    op.drop_table("item_materials")
