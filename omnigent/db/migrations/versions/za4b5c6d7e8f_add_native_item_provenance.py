"""Add durable native source identity to conversation items.

Revision ID: za4b5c6d7e8f
Revises: za3b4c5d6e7f, gb1c2d3e4f5a

The unique index is built concurrently on PostgreSQL to avoid blocking writes
to the large, active conversation_items table. The autocommit block commits
prior DDL, so the PostgreSQL column additions and index build are restart-safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "za4b5c6d7e8f"
down_revision: str | Sequence[str] | None = ("za3b4c5d6e7f", "gb1c2d3e4f5a")
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_INDEX_NAME = "uq_conversation_items_native_provenance"
_INDEX_COLUMNS = [
    "workspace_id",
    "conversation_id",
    "native_thread_id",
    "native_turn_id",
    "native_item_id",
]


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _drop_invalid_index() -> None:
    """Remove an invalid concurrent-build remnant so a retry can rebuild it."""
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND c.relnamespace = current_schema()::regnamespace "
                "AND NOT i.indisvalid"
            ),
            {"name": _INDEX_NAME},
        )
        .scalar()
    )
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")


def upgrade() -> None:
    if _is_postgres():
        # autocommit_block commits prior DDL, so IF NOT EXISTS lets a retry
        # continue if a concurrent index build failed after the columns landed.
        for name in ("native_thread_id", "native_turn_id", "native_item_id"):
            op.execute(
                f"ALTER TABLE conversation_items ADD COLUMN IF NOT EXISTS {name} VARCHAR(128)"
            )
        with op.get_context().autocommit_block():
            _drop_invalid_index()
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
                f"ON conversation_items ({', '.join(_INDEX_COLUMNS)}) "
                "WHERE native_thread_id IS NOT NULL AND native_turn_id IS NOT NULL "
                "AND native_item_id IS NOT NULL"
            )
        return

    # SQLite and other development dialects have small local tables and do not
    # support PostgreSQL's concurrent-index syntax.
    op.add_column(
        "conversation_items", sa.Column("native_thread_id", sa.String(128), nullable=True)
    )
    op.add_column("conversation_items", sa.Column("native_turn_id", sa.String(128), nullable=True))
    op.add_column("conversation_items", sa.Column("native_item_id", sa.String(128), nullable=True))
    op.create_index(
        _INDEX_NAME,
        "conversation_items",
        _INDEX_COLUMNS,
        unique=True,
        sqlite_where=sa.text(
            "native_thread_id IS NOT NULL AND native_turn_id IS NOT NULL "
            "AND native_item_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        for name in ("native_item_id", "native_turn_id", "native_thread_id"):
            op.execute(f"ALTER TABLE conversation_items DROP COLUMN IF EXISTS {name}")
        return

    op.drop_index(_INDEX_NAME, table_name="conversation_items")
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.drop_column("native_item_id")
        batch_op.drop_column("native_turn_id")
        batch_op.drop_column("native_thread_id")
