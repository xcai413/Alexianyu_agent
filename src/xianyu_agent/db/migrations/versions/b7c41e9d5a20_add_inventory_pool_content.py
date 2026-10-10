"""add inventory pool content

Revision ID: b7c41e9d5a20
Revises: 93acdeb8329d
Create Date: 2026-09-19 19:40:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b7c41e9d5a20"
down_revision: str | None = "93acdeb8329d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """text 池的发货文案与 image 池的图片地址。"""
    with op.batch_alter_table("inventory_pools", schema=None) as batch_op:
        batch_op.add_column(sa.Column("content", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("image_url", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("inventory_pools", schema=None) as batch_op:
        batch_op.drop_column("image_url")
        batch_op.drop_column("content")
