"""add item detail fields

Revision ID: 2f83ba6d41c7
Revises: 9c2e51f7ab30
Create Date: 2026-09-19 12:10:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "2f83ba6d41c7"
down_revision: str | None = "9c2e51f7ab30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add structured detail fields to the item mirror (Issue #4 Phase 4).

    images stays nullable so existing rows need no dialect-specific JSON default;
    the ORM always writes a list, and readers treat NULL as empty.
    """
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("images", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("stock", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("sold_count", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.drop_column("sold_count")
        batch_op.drop_column("stock")
        batch_op.drop_column("images")
        batch_op.drop_column("description")
