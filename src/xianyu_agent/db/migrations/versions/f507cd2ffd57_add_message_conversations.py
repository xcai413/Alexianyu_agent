"""add message conversations

Revision ID: f507cd2ffd57
Revises: b4c2e8f19a60
Create Date: 2026-09-16 02:17:47.649971+08:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f507cd2ffd57"
down_revision: str | None = "b4c2e8f19a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("external_conversation_id", sa.String(length=64), nullable=False),
        sa.Column("buyer_id", sa.String(length=64), nullable=True),
        sa.Column("item_id", sa.String(length=128), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unread_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("bot_state", sa.String(length=16), server_default="enabled", nullable=False),
        sa.Column("takeover_state", sa.String(length=16), server_default="bot", nullable=False),
        sa.Column("context_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "external_conversation_id", name="uq_conversations_account_external"
        ),
    )
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_conversations_account_buyer", ["account_id", "buyer_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_conversations_account_id"), ["account_id"], unique=False
        )
        batch_op.create_index(
            "ix_conversations_account_last_message", ["account_id", "last_message_at"], unique=False
        )

    # Add nullable columns first so existing message history remains readable while
    # the account-scoped conversation rows are backfilled below.
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("conversation_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("external_message_id", sa.String(length=128), nullable=True))

    messages = sa.table(
        "messages",
        sa.column("id", sa.Integer()),
        sa.column("account_id", sa.Integer()),
        sa.column("chat_id", sa.String()),
        sa.column("message_id", sa.String()),
        sa.column("sender_id", sa.String()),
        sa.column("direction", sa.String()),
        sa.column("item_id", sa.String()),
        sa.column("received_at", sa.DateTime(timezone=True)),
        sa.column("sent_at", sa.DateTime(timezone=True)),
        sa.column("conversation_id", sa.Integer()),
        sa.column("external_message_id", sa.String()),
    )
    conversations = sa.table(
        "conversations",
        sa.column("id", sa.Integer()),
        sa.column("account_id", sa.Integer()),
        sa.column("external_conversation_id", sa.String()),
        sa.column("buyer_id", sa.String()),
        sa.column("item_id", sa.String()),
        sa.column("last_message_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    conversation_states: dict[tuple[int, str], dict[str, object]] = {}
    rows = bind.execute(
        sa.select(
            messages.c.id,
            messages.c.account_id,
            messages.c.chat_id,
            messages.c.message_id,
            messages.c.sender_id,
            messages.c.direction,
            messages.c.item_id,
            messages.c.received_at,
            messages.c.sent_at,
        ).order_by(messages.c.id)
    ).mappings()
    for row in rows:
        key = (row["account_id"], row["chat_id"])
        observed_at = row["sent_at"] or row["received_at"]
        state = conversation_states.get(key)
        if state is None:
            bind.execute(
                conversations.insert().values(
                    account_id=row["account_id"],
                    external_conversation_id=row["chat_id"],
                    buyer_id=row["sender_id"] if row["direction"] == "inbound" else None,
                    item_id=row["item_id"],
                    last_message_at=observed_at,
                )
            )
            conversation_id = bind.execute(
                sa.select(conversations.c.id).where(
                    conversations.c.account_id == row["account_id"],
                    conversations.c.external_conversation_id == row["chat_id"],
                )
            ).scalar_one()
            assert isinstance(conversation_id, int)
            state = {
                "id": conversation_id,
                "buyer_id": row["sender_id"] if row["direction"] == "inbound" else None,
                "item_id": row["item_id"],
                "last_message_at": observed_at,
            }
            conversation_states[key] = state
        else:
            updates: dict[str, object] = {}
            if state["buyer_id"] is None and row["direction"] == "inbound" and row["sender_id"]:
                state["buyer_id"] = row["sender_id"]
                updates["buyer_id"] = row["sender_id"]
            if state["item_id"] is None and row["item_id"]:
                state["item_id"] = row["item_id"]
                updates["item_id"] = row["item_id"]
            current_observed_at = state["last_message_at"]
            if current_observed_at is None or observed_at > current_observed_at:
                state["last_message_at"] = observed_at
                updates["last_message_at"] = observed_at
            if updates:
                bind.execute(
                    conversations.update()
                    .where(conversations.c.id == state["id"])
                    .values(**updates)
                )
        bind.execute(
            messages.update()
            .where(messages.c.id == row["id"])
            .values(
                conversation_id=state["id"],
                external_message_id=row["message_id"],
            )
        )

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.alter_column("conversation_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(
            batch_op.f("ix_messages_conversation_id"), ["conversation_id"], unique=False
        )
        batch_op.create_unique_constraint(
            "uq_messages_account_external_message", ["account_id", "external_message_id"]
        )
        batch_op.create_foreign_key(
            "fk_messages_conversation_id_conversations",
            "conversations",
            ["conversation_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_constraint("fk_messages_conversation_id_conversations", type_="foreignkey")
        batch_op.drop_constraint("uq_messages_account_external_message", type_="unique")
        batch_op.drop_index(batch_op.f("ix_messages_conversation_id"))
        batch_op.drop_column("external_message_id")
        batch_op.drop_column("conversation_id")

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index("ix_conversations_account_last_message")
        batch_op.drop_index(batch_op.f("ix_conversations_account_id"))
        batch_op.drop_index("ix_conversations_account_buyer")

    op.drop_table("conversations")
