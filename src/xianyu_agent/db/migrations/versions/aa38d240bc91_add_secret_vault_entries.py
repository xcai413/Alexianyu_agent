"""add encrypted secret vault entries

Revision ID: aa38d240bc91
Revises: 83b1a1d54f70
Create Date: 2026-09-08 04:15:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa38d240bc91"
down_revision: str | None = "83b1a1d54f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret_vault_entries",
        sa.Column("ref", sa.String(length=80), nullable=False),
        sa.Column("key_version", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("ref"),
    )
    with op.batch_alter_table("secret_vault_entries", schema=None) as batch_op:
        batch_op.create_index(
            "ix_secret_vault_entries_key_version", ["key_version"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("secret_vault_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_secret_vault_entries_key_version")
    op.drop_table("secret_vault_entries")
