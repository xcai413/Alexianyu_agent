"""add conversation bargain round

Revision ID: 7b41c9e0af52
Revises: 3d9a46dcb118
Create Date: 2026-09-19 10:00:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "7b41c9e0af52"
down_revision: str | None = "3d9a46dcb118"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Track how many bargaining rounds a conversation already used."""
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("bargain_round", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("bargain_round")
