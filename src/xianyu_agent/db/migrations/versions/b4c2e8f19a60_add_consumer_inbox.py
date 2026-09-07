"""add durable consumer inbox

Revision ID: b4c2e8f19a60
Revises: aa38d240bc91
Create Date: 2026-09-08 06:15:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c2e8f19a60"
down_revision: str | None = "aa38d240bc91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consumer_inbox",
        sa.Column("consumer", sa.String(length=128), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer", "key_hash"),
    )


def downgrade() -> None:
    op.drop_table("consumer_inbox")
