"""Add durable host admission fences and operation custody.

Revision ID: gc1d2e3f4a5b
Revises: zr7a1c3e5b90
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "gc1d2e3f4a5b"
down_revision: str | None = "zr7a1c3e5b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add empty tables; enrollment requires explicit authenticated integration."""
    op.create_table(
        "host_turnover_admission",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("fence_token", Uuid16(), nullable=True),
        sa.Column("verification_sha256", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "host_id"),
        sa.CheckConstraint("workspace_id >= 0", name="ck_host_admission_workspace"),
        sa.CheckConstraint("generation >= 0", name="ck_host_admission_generation"),
        sa.CheckConstraint("state IN (1, 2)", name="ck_host_admission_state"),
        sa.CheckConstraint(
            "(state = 1 AND fence_token IS NULL) OR (state = 2 AND fence_token IS NOT NULL)",
            name="ck_host_admission_fence",
        ),
    )
    op.create_table(
        "host_turnover_operations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("token", Uuid16(), nullable=False),
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "token"),
        sa.CheckConstraint("workspace_id >= 0", name="ck_host_operation_workspace"),
        sa.CheckConstraint("generation >= 0", name="ck_host_operation_generation"),
        sa.CheckConstraint("operation IN (1, 2)", name="ck_host_operation_kind"),
        sa.CheckConstraint("state IN (1, 2, 3)", name="ck_host_operation_state"),
    )
    op.create_index(
        "ix_host_turnover_operations_scope_state",
        "host_turnover_operations",
        ["workspace_id", "host_id", "state"],
    )


def downgrade() -> None:
    """Refuse removal of any enrollment or journal, including completed history."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE host_turnover_admission, host_turnover_operations IN ACCESS EXCLUSIVE MODE"
        )
    elif bind.dialect.name == "sqlite":
        # Acquire the database write lock before the empty-table checks.
        op.execute("UPDATE host_turnover_admission SET generation = generation WHERE 1 = 0")
    else:
        raise RuntimeError("HOST_ADMISSION_DOWNGRADE_DATABASE_UNQUALIFIED")
    for table in ("host_turnover_admission", "host_turnover_operations"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("HOST_ADMISSION_DOWNGRADE_REQUIRES_EMPTY_TABLES")
    op.drop_index("ix_host_turnover_operations_scope_state", table_name="host_turnover_operations")
    op.drop_table("host_turnover_operations")
    op.drop_table("host_turnover_admission")
