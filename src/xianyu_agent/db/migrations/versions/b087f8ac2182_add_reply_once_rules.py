"""add reply once rules

Revision ID: b087f8ac2182
Revises: a2aa66513964
Create Date: 2026-09-16 18:45:02.689415+08:00

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b087f8ac2182"
down_revision: str | None = "a2aa66513964"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reply_rules", schema=None) as batch_op:
        batch_op.add_column(
            # sa.false() 在 SQLite/MySQL 渲染为 0,在 Postgres 渲染为 false;
            # 直接写 DEFAULT 0 会被 Postgres 拒绝(boolean 与 integer 类型不匹配)。
            sa.Column("reply_once", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    # ### end Alembic commands ###


def downgrade() -> None:
    with op.batch_alter_table("reply_rules", schema=None) as batch_op:
        batch_op.drop_column("reply_once")

    # ### end Alembic commands ###
