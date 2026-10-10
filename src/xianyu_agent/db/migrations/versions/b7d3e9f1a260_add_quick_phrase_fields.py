"""extend quick replies to quick phrases

Revision ID: b7d3e9f1a260
Revises: aa6f4d2c8b91
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7d3e9f1a260"
down_revision = "aa6f4d2c8b91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quick_replies", sa.Column("category", sa.String(length=64), server_default="默认", nullable=False))
    op.add_column("quick_replies", sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("quick_replies", sa.Column("use_count", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("quick_replies", "use_count")
    op.drop_column("quick_replies", "enabled")
    op.drop_column("quick_replies", "category")
