"""add lease columns for multi-replica claim

Revision ID: a4d17f2c8e35
Revises: b7c41e9d5a20
Create Date: 2026-09-20 02:30:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a4d17f2c8e35"
down_revision: str | None = "b7c41e9d5a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """实例租约(daemon_instances)与 outbox 抢占租约(M1)。

    两处租约都是"多副本不互相踩"的地基:实例租约决定谁还活着,outbox 租约决定谁有权发布。
    """
    with op.batch_alter_table("daemon_instances", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("transactional_outbox", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lease_token", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("transactional_outbox", schema=None) as batch_op:
        batch_op.drop_column("leased_until")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("lease_token")
    with op.batch_alter_table("daemon_instances", schema=None) as batch_op:
        batch_op.drop_column("lease_until")

