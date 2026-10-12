"""Add durable goals without modifying native Codex goal state."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "zd1e2f3a4b5c"
down_revision = "zc1d2e3f4a5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_goals",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("completion_evidence", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "session_id"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'blocked', 'completed', 'cancelled')",
            name="ck_session_goals_status",
        ),
        sa.CheckConstraint("revision > 0", name="ck_session_goals_revision"),
    )


def downgrade() -> None:
    op.drop_table("session_goals")
