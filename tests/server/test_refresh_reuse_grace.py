"""Refresh-token reuse grace, lost-commit recovery and family revocation.

Covers the rotation edge cases an unattended client hits when a refresh
response is lost (network blip, server crash, DB failover):

- a retry of the just-superseded token inside the grace window gets the
  same successor back (idempotent) and does not revoke;
- a retry after the window, or a replay of any older token, revokes the
  whole grant family;
- two concurrent refreshes of one token both receive the same successor;
- a rotation whose commit was rolled back (async replica promotion) is
  recovered whether the client holds the old token or the new one;
- an aged-out grant answers ``expired_token`` (not ``invalid_grant``) until
  the retention window lets the purge reclaim it.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from omnigent.server.device_grant_store import (
    DeviceGrantStore,
    derive_successor_refresh_token,
    hash_secret,
)
from tests.server.test_device_auth import _build_accounts_app, _login_admin

_KEY = b"k" * 32
_LIFETIME = 30 * 24 * 3600
_RETENTION = 7 * 24 * 3600


# ── Store level ───────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> DeviceGrantStore:
    return DeviceGrantStore(f"sqlite:///{tmp_path}/dg.db")


def _h(token: str) -> str:
    return hash_secret(token, _KEY)


def _succ(token: str, grant_id: str) -> str:
    return derive_successor_refresh_token(token, grant_id=grant_id, key=_KEY)


def _redeemed(store: DeviceGrantStore, token: str = "r0", now: int = 1000) -> str:
    grant = store.create_grant(
        secrets.token_urlsafe(8),
        device_code_hash=_h(f"dc-{token}"),
        user_code=secrets.token_urlsafe(6),
        client_id="slack",
        created_at=now,
        expires_at=now + 600,
    )
    store.approve(grant.id, user_id="a@x", now_epoch_seconds=now + 1)
    store.redeem_approved(
        grant.id,
        refresh_token_hash=_h(token),
        next_refresh_token_hash=_h(_succ(token, grant.id)),
        now_epoch_seconds=now + 2,
    )
    return grant.id


def _rotate(store: DeviceGrantStore, grant_id: str, token: str, now: int):
    new = _succ(token, grant_id)
    return new, store.rotate_refresh_token(
        grant_id,
        expected_hash=_h(token),
        new_hash=_h(new),
        next_hash=_h(_succ(new, grant_id)),
        now_epoch_seconds=now,
        max_lifetime_seconds=_LIFETIME,
    )


def test_successor_is_deterministic_and_grant_bound() -> None:
    """Same token + grant → same successor; any change → a different one."""
    a = derive_successor_refresh_token("tok", grant_id="g1", key=_KEY)
    assert a == derive_successor_refresh_token("tok", grant_id="g1", key=_KEY)
    assert a != derive_successor_refresh_token("tok", grant_id="g2", key=_KEY)
    assert a != derive_successor_refresh_token("tok2", grant_id="g1", key=_KEY)
    assert a != derive_successor_refresh_token("tok", grant_id="g1", key=b"x" * 32)
    assert len(a) == 43


def test_rotation_stamps_prev_and_retires_older(store: DeviceGrantStore) -> None:
    """Each rotation keeps the superseded token as prev and retires the one
    it displaces, so tokens older than prev stay recognisable as reuse."""
    gid = _redeemed(store)
    r1, rotated = _rotate(store, gid, "r0", now=2000)
    assert rotated is not None
    assert store.get_by_prev_refresh_hash(_h("r0")) is not None
    # Nothing is retired yet: r0 is still the previous token.
    assert store.get_by_retired_refresh_hash(_h("r0")) is None
    r2, rotated = _rotate(store, gid, r1, now=3000)
    assert rotated is not None
    assert store.get_by_prev_refresh_hash(_h(r1)) is not None
    retired = store.get_by_retired_refresh_hash(_h("r0"))
    assert retired is not None and retired.id == gid
    assert store.get_by_refresh_hash(_h(r2)) is not None


def test_grace_replay_window_bounds(store: DeviceGrantStore) -> None:
    """The replay matcher honors the window, the successor binding and 0."""
    gid = _redeemed(store)
    r1, _ = _rotate(store, gid, "r0", now=2000)
    kwargs = {"successor_hash": _h(r1), "grace_seconds": 120}
    assert store.match_grace_replay(_h("r0"), now_epoch_seconds=2120, **kwargs) is not None
    assert store.match_grace_replay(_h("r0"), now_epoch_seconds=2121, **kwargs) is None
    assert (
        store.match_grace_replay(
            _h("r0"), successor_hash=_h("other"), now_epoch_seconds=2001, grace_seconds=120
        )
        is None
    )
    assert (
        store.match_grace_replay(
            _h("r0"), successor_hash=_h(r1), now_epoch_seconds=2001, grace_seconds=0
        )
        is None
    )
    # Once the successor itself rotated, the old token is no longer replayable.
    _rotate(store, gid, r1, now=2010)
    assert store.match_grace_replay(_h("r0"), now_epoch_seconds=2011, **kwargs) is None


def test_roll_forward_recovers_rolled_back_rotation(store: DeviceGrantStore) -> None:
    """A client holding the successor of a rolled-back rotation is recovered."""
    gid = _redeemed(store)
    # The rotation r0 → r1 "committed" then was rolled back: the row still
    # says current=r0, but it carries r1's digest as the expected successor.
    r1 = _succ("r0", gid)
    assert store.get_by_refresh_hash(_h(r1)) is None
    lost = store.get_by_next_refresh_hash(_h(r1))
    assert lost is not None and lost.id == gid
    r2 = _succ(r1, gid)
    rolled = store.roll_forward_rotation(
        gid,
        presented_hash=_h(r1),
        new_hash=_h(r2),
        next_hash=_h(_succ(r2, gid)),
        now_epoch_seconds=2000,
        max_lifetime_seconds=_LIFETIME,
    )
    assert rolled is not None
    assert store.get_by_refresh_hash(_h(r2)) is not None
    assert store.get_by_prev_refresh_hash(_h(r1)) is not None
    retired = store.get_by_retired_refresh_hash(_h("r0"))
    assert retired is not None and retired.id == gid
    # Replaying the roll-forward with stale state is a no-op.
    assert (
        store.roll_forward_rotation(
            gid,
            presented_hash=_h(r1),
            new_hash=_h("x"),
            next_hash=None,
            now_epoch_seconds=2001,
            max_lifetime_seconds=_LIFETIME,
        )
        is None
    )


def test_concurrent_rotation_has_one_winner(store: DeviceGrantStore) -> None:
    """Racing rotations of one token: exactly one commits, the other can
    then be answered through the grace matcher with the same successor."""
    gid = _redeemed(store)
    r1 = _succ("r0", gid)

    def _attempt(_: int):
        return _rotate(store, gid, "r0", now=2000)[1]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_attempt, range(4)))
    assert sum(r is not None for r in results) == 1
    assert (
        store.match_grace_replay(
            _h("r0"), successor_hash=_h(r1), now_epoch_seconds=2001, grace_seconds=120
        )
        is not None
    )


def test_revoke_clears_grace_state(store: DeviceGrantStore) -> None:
    gid = _redeemed(store)
    r1, _ = _rotate(store, gid, "r0", now=2000)
    assert store.revoke(gid) is True
    assert store.get_by_prev_refresh_hash(_h("r0")) is None
    assert store.get_by_next_refresh_hash(_h(_succ(r1, gid))) is None
    assert (
        store.match_grace_replay(
            _h("r0"), successor_hash=_h(r1), now_epoch_seconds=2001, grace_seconds=120
        )
        is None
    )


def test_purge_keeps_aged_grants_for_retention(store: DeviceGrantStore) -> None:
    """Aged grants survive the retention window; retired digests go with them."""
    gid = _redeemed(store, now=1000)
    r1, _ = _rotate(store, gid, "r0", now=2000)
    _rotate(store, gid, r1, now=3000)  # retires r0 at t=3000
    aged = 1001 + _LIFETIME + 1
    assert (
        store.purge_expired(aged, max_lifetime_seconds=_LIFETIME, retention_seconds=_RETENTION)
        == 0
    )
    assert store.get_by_id(gid) is not None
    gone = 3000 + _LIFETIME + _RETENTION + 1
    assert (
        store.purge_expired(gone, max_lifetime_seconds=_LIFETIME, retention_seconds=_RETENTION)
        == 1
    )
    assert store.get_by_id(gid) is None
    assert store.get_by_retired_refresh_hash(_h("r0")) is None


# ── Route level ───────────────────────────────────────────────────


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("OMNIGENT_REFRESH_REUSE_GRACE_SECONDS", raising=False)
    yield from _build_accounts_app(tmp_path, monkeypatch)


@pytest.fixture
def strict_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("OMNIGENT_REFRESH_REUSE_GRACE_SECONDS", "0")
    yield from _build_accounts_app(tmp_path, monkeypatch)


class _Clock:
    """Shifts ``time.time`` as seen by the token routes only."""

    def __init__(self) -> None:
        self.offset = 0.0

    def time(self) -> float:
        return time.time() + self.offset


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    from omnigent.server.routes import device_auth

    shifted = _Clock()
    monkeypatch.setattr(device_auth, "time", SimpleNamespace(time=shifted.time))
    return shifted


def _device_refresh_token(client: TestClient) -> str:
    """Run the device flow once and return the first (rotating) refresh token."""
    r = client.post("/oauth/device/authorize", json={"client_id": "slack"})
    assert r.status_code == 200, r.text
    device_code, user_code = r.json()["device_code"], r.json()["user_code"]
    _login_admin(client)
    r = client.post(
        "/oauth/device/approve",
        data={"user_code": user_code},
        headers={"Origin": "http://localhost:8000"},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/oauth/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
        },
    )
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()["refresh_token"]


def _refresh(client: TestClient, token: str):
    return client.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token}
    )


def test_retry_within_grace_returns_same_successor(app: TestClient) -> None:
    """A lost-response retry gets the identical successor and keeps the grant."""
    r0 = _device_refresh_token(app)
    first = _refresh(app, r0)
    assert first.status_code == 200, first.text
    r1 = first.json()["refresh_token"]
    retry = _refresh(app, r0)
    assert retry.status_code == 200, retry.text
    assert retry.json()["refresh_token"] == r1
    assert retry.json()["access_token"]
    # The grant is intact: the successor keeps rotating normally.
    nxt = _refresh(app, r1)
    assert nxt.status_code == 200, nxt.text
    assert nxt.json()["refresh_token"] not in (r0, r1)


def test_reuse_after_grace_revokes_family(app: TestClient, clock: _Clock) -> None:
    """Past the window the previous token is theft: the grant is revoked."""
    r0 = _device_refresh_token(app)
    r1 = _refresh(app, r0).json()["refresh_token"]
    clock.offset = 121
    reuse = _refresh(app, r0)
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    # The legitimately rotated token died with the family.
    assert _refresh(app, r1).status_code == 400


def test_grace_disabled_restores_strict_single_use(strict_app: TestClient) -> None:
    r0 = _device_refresh_token(strict_app)
    r1 = _refresh(strict_app, r0).json()["refresh_token"]
    assert _refresh(strict_app, r0).status_code == 400
    assert _refresh(strict_app, r1).status_code == 400


def test_older_than_previous_token_revokes_family(app: TestClient) -> None:
    """A token two rotations old is reuse even inside the grace window."""
    r0 = _device_refresh_token(app)
    r1 = _refresh(app, r0).json()["refresh_token"]
    r2 = _refresh(app, r1).json()["refresh_token"]
    reuse = _refresh(app, r0)
    assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
    assert _refresh(app, r2).status_code == 400


def test_grace_replay_after_successor_used_revokes(app: TestClient) -> None:
    """Once the successor has rotated, the old token is no longer a retry."""
    r0 = _device_refresh_token(app)
    r1 = _refresh(app, r0).json()["refresh_token"]
    r2 = _refresh(app, r1).json()["refresh_token"]
    # r1 is now the previous token and r2 its successor: a retry of r1 is
    # still idempotent...
    assert _refresh(app, r1).json()["refresh_token"] == r2
    # ...but r0 (older than previous) revokes.
    assert _refresh(app, r0).status_code == 400
    assert _refresh(app, r2).status_code == 400


def test_concurrent_double_refresh_converges(app: TestClient) -> None:
    """Two refreshes of one token in flight together get the same successor."""
    r0 = _device_refresh_token(app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _refresh(app, r0), range(2)))
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    tokens = {r.json()["refresh_token"] for r in responses}
    assert len(tokens) == 1
    assert _refresh(app, tokens.pop()).status_code == 200


def _grant_state(db: Path, grant_id: str) -> dict[str, object]:
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT refresh_token_hash, prev_refresh_token_hash, prev_rotated_at, "
                "next_refresh_token_hash FROM device_grants WHERE id = :id"
            ),
            {"id": grant_id},
        ).one()
    engine.dispose()
    return dict(row._mapping)


def _restore_grant_state(db: Path, grant_id: str, state: dict[str, object]) -> None:
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE device_grants SET refresh_token_hash = :refresh_token_hash, "
                "prev_refresh_token_hash = :prev_refresh_token_hash, "
                "prev_rotated_at = :prev_rotated_at, "
                "next_refresh_token_hash = :next_refresh_token_hash WHERE id = :id"
            ),
            {**state, "id": grant_id},
        )
        conn.execute(sa.text("DELETE FROM device_grant_retired_refresh_tokens"))
    engine.dispose()


def _only_grant_id(db: Path) -> str:
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        (grant_id,) = conn.execute(
            sa.text("SELECT id FROM device_grants WHERE client_id = 'slack'")
        ).one()
    engine.dispose()
    return str(grant_id)


@pytest.mark.parametrize("client_kept", ["old_token", "new_token"])
def test_rolled_back_rotation_recovers(app: TestClient, tmp_path: Path, client_kept: str) -> None:
    """An async failover can roll back a committed rotation. Whether the
    client kept the old token (response lost) or the new one (response
    delivered), the grant keeps working without a re-login."""
    db = tmp_path / "test.db"
    r0 = _device_refresh_token(app)
    gid = _only_grant_id(db)
    before = _grant_state(db, gid)
    r1 = _refresh(app, r0).json()["refresh_token"]
    _restore_grant_state(db, gid, before)  # the rotation commit is lost

    if client_kept == "old_token":
        again = _refresh(app, r0)
        assert again.status_code == 200, again.text
        assert again.json()["refresh_token"] == r1
        follow = _refresh(app, r1)
    else:
        follow = _refresh(app, r1)
    assert follow.status_code == 200, follow.text
    r2 = follow.json()["refresh_token"]
    assert r2 not in (r0, r1)
    assert _refresh(app, r2).status_code == 200


def test_rolled_forward_successor_is_grace_replayable(app: TestClient, tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    r0 = _device_refresh_token(app)
    gid = _only_grant_id(db)
    before = _grant_state(db, gid)
    r1 = _refresh(app, r0).json()["refresh_token"]
    _restore_grant_state(db, gid, before)
    r2 = _refresh(app, r1).json()["refresh_token"]
    # Retrying the roll-forward request inside the window is idempotent.
    assert _refresh(app, r1).json()["refresh_token"] == r2
    # The rolled-back current token is retired: replaying it revokes.
    assert _refresh(app, r0).status_code == 400
    assert _refresh(app, r2).status_code == 400


def test_refresh_reports_grant_lifetime(app: TestClient) -> None:
    r0 = _device_refresh_token(app)
    body = _refresh(app, r0).json()
    assert 0 < body["refresh_token_expires_in"] <= _LIFETIME


def _login_refresh_token(client: TestClient) -> tuple[str, dict[str, object]]:
    r = client.post(
        "/auth/login",
        json={"username": "admin", "password": "admin-pw-12345", "issue_refresh": True},
    )
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r.json()["refresh_token"], r.json()


def test_login_grant_reports_lifetime_and_does_not_rotate(app: TestClient) -> None:
    token, login_body = _login_refresh_token(app)
    assert login_body["refresh_token_expires_in"] == _LIFETIME
    r = _refresh(app, token)
    assert r.status_code == 200, r.text
    assert r.json()["refresh_token"] == token
    assert _LIFETIME - 60 <= r.json()["refresh_token_expires_in"] <= _LIFETIME


def test_aged_login_grant_says_expired_until_purged(app: TestClient, clock: _Clock) -> None:
    """An aged-out grant answers ``expired_token`` (actionable: re-login)
    while retained, and only an unknown token after the purge."""
    token, _ = _login_refresh_token(app)
    clock.offset = _LIFETIME + 60
    aged = _refresh(app, token)
    assert aged.status_code == 400 and aged.json()["error"] == "expired_token"
    # Past the retention window the throttled purge reclaims the row.
    clock.offset = _LIFETIME + _RETENTION + 3600
    gone = _refresh(app, token)
    assert gone.status_code == 400 and gone.json()["error"] == "invalid_grant"


def test_grace_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.server.routes.device_auth import _refresh_reuse_grace_seconds

    monkeypatch.delenv("OMNIGENT_REFRESH_REUSE_GRACE_SECONDS", raising=False)
    assert _refresh_reuse_grace_seconds() == 120
    for raw, expected in (("0", 0), ("30", 30), ("-5", 0), ("bogus", 120), ("99999", 600)):
        monkeypatch.setenv("OMNIGENT_REFRESH_REUSE_GRACE_SECONDS", raw)
        assert _refresh_reuse_grace_seconds() == expected
