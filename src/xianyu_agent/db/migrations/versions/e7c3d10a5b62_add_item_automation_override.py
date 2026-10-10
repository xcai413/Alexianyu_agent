"""add per-item automation override

Revision ID: e7c3d10a5b62
Revises: d2f5b81c7a94
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e7c3d10a5b62"
down_revision = "d2f5b81c7a94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items", sa.Column("automation_override", sa.String(length=16), nullable=True)
    )
    op.add_column("items", sa.Column("automation_note", sa.String(length=255), nullable=True))
    op.add_column(
        "items",
        sa.Column("automation_overridden_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "items", sa.Column("automation_overridden_by", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("items", "automation_overridden_by")
    op.drop_column("items", "automation_overridden_at")
    op.drop_column("items", "automation_note")
    op.drop_column("items", "automation_override")
