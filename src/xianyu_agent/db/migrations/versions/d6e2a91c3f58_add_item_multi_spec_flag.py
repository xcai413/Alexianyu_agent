"""add item multi spec flag

Revision ID: d6e2a91c3f58
Revises: c4b7de05f19a
Create Date: 2026-09-19 15:40:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d6e2a91c3f58"
down_revision: str | None = "c4b7de05f19a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remember whether a mirrored item is multi-spec; NULL means not fetched yet."""
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("has_multi_spec", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.drop_column("has_multi_spec")
