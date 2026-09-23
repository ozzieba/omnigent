"""Add durable native source identity to conversation items.

Revision ID: za4b5c6d7e8f
Revises: za3b4c5d6e7f
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "za4b5c6d7e8f"
down_revision: str | Sequence[str] | None = ("za3b4c5d6e7f", "gb1c2d3e4f5a")
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_items", sa.Column("native_thread_id", sa.String(128), nullable=True)
    )
    op.add_column("conversation_items", sa.Column("native_turn_id", sa.String(128), nullable=True))
    op.add_column("conversation_items", sa.Column("native_item_id", sa.String(128), nullable=True))
    op.create_index(
        "uq_conversation_items_native_provenance",
        "conversation_items",
        [
            "workspace_id",
            "conversation_id",
            "native_thread_id",
            "native_turn_id",
            "native_item_id",
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_conversation_items_native_provenance", table_name="conversation_items")
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.drop_column("native_item_id")
        batch_op.drop_column("native_turn_id")
        batch_op.drop_column("native_thread_id")
