"""Persist immutable agent environment profile references.

Revision ID: za3b4c5d6e7f
Revises: za2b3c4d5e6f
Create Date: 2026-09-23 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "za3b4c5d6e7f"
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable immutable profile reference to session metadata."""
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }
    if "environment_profile" not in existing:
        op.add_column(
            "omnigent_conversation_metadata",
            sa.Column("environment_profile", sa.String(128), nullable=True),
        )


def downgrade() -> None:
    """Remove the profile reference."""
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }
    if "environment_profile" in existing:
        with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
            batch_op.drop_column("environment_profile")
