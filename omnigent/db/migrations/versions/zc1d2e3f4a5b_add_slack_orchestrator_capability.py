"""Add restricted device-grant capability state.

Revision ID: zc1d2e3f4a5b
Revises: zb1c2d3e4f5a
Create Date: 2026-10-01 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zc1d2e3f4a5b"
down_revision: str | Sequence[str] | None = "zb1c2d3e4f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("device_grants", sa.Column("capability_json", sa.Text(), nullable=True))
    op.add_column(
        "device_grants",
        sa.Column("approved_by_user_id", sa.String(length=128), nullable=True),
    )
    op.create_table(
        "device_grant_sessions",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("grant_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "grant_id", "session_id", name="pk_device_grant_sessions"
        ),
        sa.UniqueConstraint("workspace_id", "session_id", name="uq_device_grant_sessions_session"),
    )


def downgrade() -> None:
    op.drop_table("device_grant_sessions")
    # Batched so the downgrade also works on SQLite (no raw DROP COLUMN).
    with op.batch_alter_table("device_grants") as batch_op:
        batch_op.drop_column("approved_by_user_id")
        batch_op.drop_column("capability_json")
