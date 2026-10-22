"""Track accepted work for scheduled task runs.

Revision ID: zb2c3d4e5f6a
Revises: zd1e2f3a4b5c
Create Date: 2026-09-29 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "zb2c3d4e5f6a"
down_revision: str | None = "zd1e2f3a4b5c"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("scheduled_task_runs", sa.Column("started_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.drop_column("started_at")
