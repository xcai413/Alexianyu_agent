"""add risk hit count

Revision ID: e5c3a72f9b04
Revises: d6e2a91c3f58
Create Date: 2026-09-19 16:20:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e5c3a72f9b04"
down_revision: str | None = "d6e2a91c3f58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Track consecutive risk hits so the cooldown can escalate."""
    with op.batch_alter_table("worker_status", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("risk_hit_count", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("worker_status", schema=None) as batch_op:
        batch_op.drop_column("risk_hit_count")
