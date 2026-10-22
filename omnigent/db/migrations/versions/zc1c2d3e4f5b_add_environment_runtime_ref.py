"""Persist the resolved managed-host image catalog revision.

Revision ID: zc1c2d3e4f5b
Revises: zr7a1c3e5b90
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zc1c2d3e4f5b"
down_revision: str | Sequence[str] | None = "zr7a1c3e5b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable runtime image reference to session metadata."""
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }
    if "environment_runtime_ref" not in existing:
        op.add_column(
            "omnigent_conversation_metadata",
            sa.Column("environment_runtime_ref", sa.String(128), nullable=True),
        )


def downgrade() -> None:
    """Remove the runtime image reference."""
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }
    if "environment_runtime_ref" in existing:
        with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
            batch_op.drop_column("environment_runtime_ref")
