"""Add refresh-token reuse-grace state to device grants.

Additive only: four nullable columns on ``device_grants`` plus a new
``device_grant_retired_refresh_tokens`` table. Existing rows keep working
unchanged (NULL means "no rotation recorded yet").

- ``prev_rotated_at``: when ``prev_refresh_token_hash`` was superseded,
  bounding the window in which a retried refresh of the previous token is
  answered idempotently instead of being treated as theft.
- ``next_refresh_token_hash``: digest of the current token's deterministic
  successor, so a successor whose rotation commit was lost (async replica
  promotion) is recognised and rolled forward.
- ``prev_replay_count`` / ``replay_streak``: grace replays answered for the
  current previous token, and consecutive rotations that needed one.
- ``device_grant_retired_refresh_tokens``: digests of tokens older than the
  previous one, so replaying any of them revokes the grant family.

Revision ID: zr7a1c3e5b90
Revises: zc1d2e3f4a5b
Create Date: 2026-10-21 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zr7a1c3e5b90"
down_revision: str | Sequence[str] | None = "zc1d2e3f4a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("device_grants", sa.Column("prev_rotated_at", sa.Integer(), nullable=True))
    op.add_column(
        "device_grants",
        sa.Column("next_refresh_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column("device_grants", sa.Column("prev_replay_count", sa.Integer(), nullable=True))
    op.add_column("device_grants", sa.Column("replay_streak", sa.Integer(), nullable=True))
    op.create_index(
        "ix_device_grants_next_refresh_hash",
        "device_grants",
        ["workspace_id", "next_refresh_token_hash"],
    )
    op.create_table(
        "device_grant_retired_refresh_tokens",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("grant_id", sa.String(length=128), nullable=False),
        sa.Column("retired_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "token_hash", name="pk_device_grant_retired_refresh_tokens"
        ),
    )
    op.create_index(
        "ix_device_grant_retired_refresh_tokens_grant",
        "device_grant_retired_refresh_tokens",
        ["workspace_id", "grant_id"],
    )
    op.create_index(
        "ix_device_grant_retired_refresh_tokens_retired_at",
        "device_grant_retired_refresh_tokens",
        ["workspace_id", "retired_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_device_grant_retired_refresh_tokens_retired_at",
        table_name="device_grant_retired_refresh_tokens",
    )
    op.drop_index(
        "ix_device_grant_retired_refresh_tokens_grant",
        table_name="device_grant_retired_refresh_tokens",
    )
    op.drop_table("device_grant_retired_refresh_tokens")
    op.drop_index("ix_device_grants_next_refresh_hash", table_name="device_grants")
    # Batched so the downgrade also works on SQLite (no raw DROP COLUMN).
    with op.batch_alter_table("device_grants") as batch_op:
        batch_op.drop_column("replay_streak")
        batch_op.drop_column("prev_replay_count")
        batch_op.drop_column("next_refresh_token_hash")
        batch_op.drop_column("prev_rotated_at")
