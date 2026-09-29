"""Merge the personal quota-route and environment-profile migration heads.

The environment-profile column (za3b4c5d6e7f) branched from za2b3c4d5e6f
on the sandbox line while main advanced through za1b2c3d4e5f,
ga1b2c3d4e5f and gb1c2d3e4f5a. Merging both lines left two heads, so
``alembic upgrade head`` refused to run and the server could not boot.
This no-op revision joins them; databases at either head upgrade cleanly.

Revision ID: zb1c2d3e4f5a
Revises: gb1c2d3e4f5a, za3b4c5d6e7f
Create Date: 2026-09-29 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "zb1c2d3e4f5a"
down_revision: str | Sequence[str] | None = ("gb1c2d3e4f5a", "za3b4c5d6e7f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No schema change; joins the two heads."""


def downgrade() -> None:
    """No schema change; splits back into the two heads."""
