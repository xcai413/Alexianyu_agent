"""add batch publish tables

Revision ID: 5a7c40e93b18
Revises: 2f83ba6d41c7
Create Date: 2026-09-19 13:05:00.000000+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "5a7c40e93b18"
down_revision: str | None = "2f83ba6d41c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist batch publish jobs and their per-row drafts (Issue #4 M13)."""
    op.create_table(
        "batch_publish_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("pending", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "batch_publish_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("external_item_id", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["batch_publish_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "row_index", name="uq_batch_publish_entries_job_row"),
    )
    with op.batch_alter_table("batch_publish_jobs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_batch_publish_jobs_account_id"), ["account_id"], unique=False
        )
    with op.batch_alter_table("batch_publish_entries", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_batch_publish_entries_job_id"), ["job_id"], unique=False
        )
        batch_op.create_index(
            "ix_batch_publish_entries_job_state", ["job_id", "state"], unique=False
        )


def downgrade() -> None:
    op.drop_table("batch_publish_entries")
    op.drop_table("batch_publish_jobs")
