"""add account-level reply pause

Revision ID: a1c7f3d84b26
Revises: f4b8e26a9c17
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1c7f3d84b26"
down_revision = "f4b8e26a9c17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts", sa.Column("reply_paused_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "accounts", sa.Column("reply_paused_reason", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("accounts", "reply_paused_reason")
    op.drop_column("accounts", "reply_paused_until")
