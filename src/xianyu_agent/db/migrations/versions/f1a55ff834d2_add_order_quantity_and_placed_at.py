"""add order quantity and placed at

Revision ID: f1a55ff834d2
Revises: c55a71e3a2b4
Create Date: 2026-08-21 00:00:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a55ff834d2"
down_revision: str | None = "c55a71e3a2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("quantity", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            "ix_orders_account_item_status",
            ["account_id", "item_id", "status"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index("ix_orders_account_item_status")
        batch_op.drop_column("placed_at")
        batch_op.drop_column("quantity")
