"""add delivery template library and policy template_ref

Revision ID: d2f5b81c7a94
Revises: c91a72e4f603
Create Date: 2026-09-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d2f5b81c7a94"
down_revision = "c91a72e4f603"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_templates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_delivery_templates_name"),
    )
    op.add_column(
        "delivery_policies",
        sa.Column("template_ref", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("delivery_policies", "template_ref")
    op.drop_table("delivery_templates")
