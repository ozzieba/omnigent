"""Tests for the refresh-token reuse-grace migration (zr7a1c3e5b90).

The migration is additive: two nullable columns on ``device_grants`` and the
``device_grant_retired_refresh_tokens`` table. A grant written before it must
survive the upgrade unchanged (NULL grace state), and the downgrade must
remove exactly what the upgrade added.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "zc1d2e3f4a5b"
_THIS = "zr7a1c3e5b90"


def _run(uri: str, engine: sa.Engine, fn: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, fn)(config, revision)


def test_upgrade_is_additive_and_downgrade_reverts(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'grace.db'}"
    engine = sa.create_engine(uri)
    _run(uri, engine, "upgrade", _PREVIOUS)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO device_grants (workspace_id, id, device_code_hash, user_code, "
                "status, client_id, user_id, refresh_token_hash, created_at, expires_at, "
                "approved_at) VALUES (0, 'g1', 'dch', 'uc', 4, 'omnigent-cli', 'admin', "
                "'rth', 1, 1, 1)"
            )
        )

    _run(uri, engine, "upgrade", _THIS)
    inspector = sa.inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("device_grants")}
    assert columns["prev_rotated_at"]["nullable"]
    assert columns["next_refresh_token_hash"]["nullable"]
    index_names = {i["name"] for i in inspector.get_indexes("device_grants")}
    assert "ix_device_grants_next_refresh_hash" in index_names
    assert "device_grant_retired_refresh_tokens" in inspector.get_table_names()
    pk = inspector.get_pk_constraint("device_grant_retired_refresh_tokens")
    assert set(pk["constrained_columns"]) == {"workspace_id", "token_hash"}
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT refresh_token_hash, prev_rotated_at, next_refresh_token_hash "
                "FROM device_grants WHERE id = 'g1'"
            )
        ).one()
    assert tuple(row) == ("rth", None, None)

    _run(uri, engine, "downgrade", _PREVIOUS)
    inspector = sa.inspect(engine)
    assert "device_grant_retired_refresh_tokens" not in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("device_grants")}
    assert "prev_rotated_at" not in columns and "next_refresh_token_hash" not in columns
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM device_grants")).scalar() == 1

    engine.dispose()
    clear_engine_cache()
