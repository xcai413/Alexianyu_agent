"""add account ownership lease

Revision ID: d9c4a7e15b32
Revises: a4d17f2c8e35
Create Date: 2026-09-20 03:20:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d9c4a7e15b32"
down_revision: str | None = "a4d17f2c8e35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """账号归属租约(M2):同一账号任一时刻最多一个未过期 owner。"""
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_instance_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("owner_lease_until", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("owner_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_accounts_owner_instance_id", ["owner_instance_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_index("ix_accounts_owner_instance_id")
        batch_op.drop_column("owner_heartbeat_at")
        batch_op.drop_column("owner_lease_until")
        batch_op.drop_column("owner_instance_id")

