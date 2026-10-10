"""worker_commands 增加 payload 列,承载人工回复等需传参的命令。

Revision ID: b7d13c9f4a21
Revises: e1a5c93b7d42
Create Date: 2026-09-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7d13c9f4a21"
down_revision = "e1a5c93b7d42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_commands", sa.Column("payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("worker_commands", "payload")
