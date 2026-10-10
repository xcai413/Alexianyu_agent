"""add item variants

Revision ID: 9c2e51f7ab30
Revises: 7b41c9e0af52
Create Date: 2026-09-19 11:30:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9c2e51f7ab30"
down_revision: str | None = "7b41c9e0af52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist structured multi-spec units for a mirrored item (Issue #4 M10)."""
    op.create_table(
        "item_variants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("item_pk", sa.Integer(), nullable=False),
        sa.Column("external_sku_id", sa.String(length=128), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("price", sa.String(length=64), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("image_url", sa.String(length=1024), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["item_pk"], ["items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_pk", "external_sku_id", name="uq_item_variants_item_sku"),
    )
    with op.batch_alter_table("item_variants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_item_variants_item_pk"), ["item_pk"], unique=False)
        batch_op.create_index("ix_item_variants_item_enabled", ["item_pk", "enabled"], unique=False)


def downgrade() -> None:
    op.drop_table("item_variants")
