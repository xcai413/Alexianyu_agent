"""add ai providers

Revision ID: 3d9a46dcb118
Revises: b087f8ac2182
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3d9a46dcb118"
down_revision: str | None = "b087f8ac2182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_providers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("secret_ref", sa.String(length=80), nullable=True),
        sa.Column("default_model", sa.String(length=128), nullable=False),
        # 见 b087f8ac2182:布尔列的默认值必须用 sa.false(),否则 Postgres 拒绝 DEFAULT 0。
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
        # MySQL 不允许 TEXT/JSON 列带默认值(错误 1101);默认值改由模型侧 default=dict 提供,
        # 插入路径(provider_service.create)始终显式写入 extra_config。
        sa.Column("extra_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ai_providers_name", "ai_providers", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_ai_providers_name", table_name="ai_providers")
    op.drop_table("ai_providers")
