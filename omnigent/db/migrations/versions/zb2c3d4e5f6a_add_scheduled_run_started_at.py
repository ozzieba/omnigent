"""Track accepted work for scheduled task runs.

Revision ID: zb2c3d4e5f6a
Revises: zb1c2d3e4f5a
Create Date: 2026-09-29 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "zb2c3d4e5f6a"
down_revision: str | None = "zb1c2d3e4f5a"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("scheduled_task_runs", sa.Column("started_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_task_runs", "started_at")
