"""add item spec bindings

Revision ID: a8d1f5c70e42
Revises: 5a7c40e93b18
Create Date: 2026-09-19 14:20:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a8d1f5c70e42"
down_revision: str | None = "5a7c40e93b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Map each spec combination of an item to one inventory source (Issue #4 M10)."""
    op.create_table(
        "item_spec_bindings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_pk", sa.Integer(), nullable=False),
        sa.Column("spec_key", sa.String(length=512), nullable=False),
        sa.Column("specs", sa.JSON(), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["item_pk"], ["items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_pk", "spec_key", name="uq_item_spec_bindings_item_spec"),
    )
    with op.batch_alter_table("item_spec_bindings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_item_spec_bindings_item_pk"), ["item_pk"], unique=False
        )
        batch_op.create_index(
            "ix_item_spec_bindings_item_enabled", ["item_pk", "enabled"], unique=False
        )


def downgrade() -> None:
    op.drop_table("item_spec_bindings")
