"""merge notification and message filter migration branches

Revision ID: aa6f4d2c8b91
Revises: 9c2e7f1a4b60, c3f7a52e9b14
"""

from __future__ import annotations

revision = "aa6f4d2c8b91"
down_revision = ("9c2e7f1a4b60", "c3f7a52e9b14")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge the two already-applied linear branches."""


def downgrade() -> None:
    """Merge downgrade is intentionally empty; Alembic follows both parents."""
