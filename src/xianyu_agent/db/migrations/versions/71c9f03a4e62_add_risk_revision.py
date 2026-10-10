"""为风控恢复增加独立版本,防止旧确认解除新风控。"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "71c9f03a4e62"
down_revision = "3fad1e1165dc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("worker_status") as batch:
        batch.add_column(sa.Column("risk_revision", sa.String(32), nullable=True))
    status = sa.table(
        "worker_status",
        sa.column("id", sa.Integer()),
        sa.column("risk_recovery_required", sa.Boolean()),
        sa.column("risk_revision", sa.String(32)),
    )
    bind = op.get_bind()
    ids = bind.scalars(sa.select(status.c.id).where(status.c.risk_recovery_required.is_(True)))
    for row_id in list(ids):
        bind.execute(
            status.update().where(status.c.id == row_id).values(risk_revision=uuid.uuid4().hex)
        )


def downgrade() -> None:
    with op.batch_alter_table("worker_status") as batch:
        batch.drop_column("risk_revision")
