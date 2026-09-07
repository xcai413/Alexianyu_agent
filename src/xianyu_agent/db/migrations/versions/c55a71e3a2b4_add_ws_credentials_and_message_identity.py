"""add ws credentials and account-scoped message identity

Revision ID: c55a71e3a2b4
Revises: 9a7e615f2c41
Create Date: 2026-08-14 04:30:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c55a71e3a2b4"
down_revision: str | None = "9a7e615f2c41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _message_id_unique_constraint_name() -> str:
    """Resolve the physical unique-constraint name across supported dialects."""
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("messages"):
        columns = list(constraint.get("column_names") or [])
        name = constraint.get("name")
        if columns == ["message_id"] and isinstance(name, str) and name:
            return name

    # SQLite can reflect an unnamed UNIQUE constraint. batch_alter_table applies
    # the naming convention below, making this synthetic name addressable.
    return "uq_messages_message_id"


def upgrade() -> None:
    op.create_table(
        "ws_credentials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id"),
    )
    message_id_unique = _message_id_unique_constraint_name()
    with op.batch_alter_table(
        "messages", schema=None, naming_convention=_NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_constraint(message_id_unique, type_="unique")
        batch_op.add_column(sa.Column("item_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_messages_item_id", ["item_id"], unique=False)
        batch_op.create_unique_constraint(
            "uq_messages_account_message", ["account_id", "message_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "messages", schema=None, naming_convention=_NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_constraint("uq_messages_account_message", type_="unique")
        batch_op.drop_index("ix_messages_item_id")
        batch_op.drop_column("sent_at")
        batch_op.drop_column("item_id")
        batch_op.create_unique_constraint("uq_messages_message_id", ["message_id"])
    op.drop_table("ws_credentials")
