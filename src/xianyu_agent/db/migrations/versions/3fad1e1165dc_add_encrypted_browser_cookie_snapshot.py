"""add encrypted browser cookie snapshot

Revision ID: 3fad1e1165dc
Revises: b7d3e9f1a260
Create Date: 2026-09-21 22:50:10.873798+08:00

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3fad1e1165dc'
down_revision: str | None = 'b7d3e9f1a260'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cookies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("encrypted_snapshot", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cookies", schema=None) as batch_op:
        batch_op.drop_column("encrypted_snapshot")
