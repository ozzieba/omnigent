"""Positive-auth binding resolution with real workspace-scoped host reads."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import sqlalchemy as sa
from fastapi import HTTPException, Request

from omnigent.db.db_models import SqlHost, current_workspace_id, workspace_scope
from omnigent.db.utils import make_named_managed_session_maker
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._host_admission import resolve_admission_binding
from omnigent.stores.host_admission_store import Binding, Refused, Store, hosts, operations
from omnigent.stores.host_store import HostStore

HOST = "1" * 32
ALICE = "alice@example.test"
BOB = "bob@example.test"


class FixtureAuth(AuthProvider):
    """Supply the verified-subject contract without performing external login."""

    def __init__(self, subject):
        self.subject = subject

    def get_user_id(self, request):
        return self.subject


class AuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="host-admission-authority-")
        self.engine = sa.create_engine(f"sqlite:///{Path(self.directory.name) / 'private.db'}")
        for table in (SqlHost.__table__, hosts, operations):
            table.create(self.engine)
        with self.engine.begin() as conn:
            for workspace, owner in ((0, BOB), (7, ALICE)):
                conn.execute(
                    SqlHost.__table__.insert().values(
                        workspace_id=workspace,
                        host_id=HOST,
                        user_id=owner,
                        name="synthetic",
                        status=1,
                        created_at=1,
                        updated_at=1,
                    )
                )
        self.host_store = HostStore.__new__(HostStore)
        self.host_store._session = make_named_managed_session_maker(
            self.engine, query_name_prefix="test.authorized_host"
        )
        self.request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/hosts/maintenance",
                "headers": [(b"x-owner", b"bob@example.test"), (b"x-workspace", b"0")],
            }
        )
        self.worker_observations = []

        @sa.event.listens_for(self.engine, "before_cursor_execute")
        def observe(conn, cursor, statement, parameters, context, executemany):
            self.worker_observations.append((threading.get_ident(), current_workspace_id()))

    async def asyncTearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    async def resolve(self, provider, host_id=HOST):
        return await resolve_admission_binding(
            self.request, auth_provider=provider, host_store=self.host_store, host_id=host_id
        )

    async def test_disabled_and_missing_auth_refuse_before_database_read(self):
        with self.assertRaises(HTTPException) as disabled:
            await self.resolve(None)
        self.assertEqual(disabled.exception.status_code, 401)
        with self.assertRaises(OmnigentError):
            await self.resolve(FixtureAuth(None))
        self.assertEqual(self.worker_observations, [])

    async def test_reserved_or_empty_subjects_refuse_before_database_read(self):
        for subject in ("local", "__public__", "", False):
            with self.subTest(subject=subject), self.assertRaises(HTTPException) as refusal:
                await self.resolve(FixtureAuth(subject))
            self.assertEqual(refusal.exception.status_code, 401)
        self.assertEqual(self.worker_observations, [])

    async def test_trusted_workspace_and_subject_override_untrusted_headers(self):
        main_thread = threading.get_ident()
        with workspace_scope(7):
            binding = await self.resolve(FixtureAuth(ALICE), "host_" + HOST)
        self.assertEqual(binding, Binding(7, HOST, ALICE))
        self.assertEqual(current_workspace_id(), 0)
        self.assertTrue(self.worker_observations)
        self.assertEqual({workspace for _, workspace in self.worker_observations}, {7})
        self.assertTrue(all(thread != main_thread for thread, _ in self.worker_observations))

    async def test_wrong_owner_and_other_workspace_are_denied(self):
        with self.assertRaises(HTTPException) as wrong_owner:
            await self.resolve(FixtureAuth(ALICE))
        self.assertEqual(wrong_owner.exception.status_code, 403)
        with workspace_scope(8), self.assertRaises(HTTPException) as missing:
            await self.resolve(FixtureAuth(ALICE))
        self.assertEqual(missing.exception.status_code, 404)

    async def test_malformed_host_refuses_before_database_read(self):
        with self.assertRaises(HTTPException) as malformed:
            await self.resolve(FixtureAuth(ALICE), "invalid")
        self.assertEqual(malformed.exception.status_code, 404)
        self.assertEqual(self.worker_observations, [])

    async def test_owner_changed_after_resolution_refuses_durable_admission(self):
        with workspace_scope(7):
            binding = await self.resolve(FixtureAuth(ALICE))
        store = Store(self.engine)
        store.enroll(binding)
        with self.engine.begin() as conn:
            conn.execute(
                SqlHost.__table__.update()
                .where(SqlHost.workspace_id == 7, SqlHost.host_id == HOST)
                .values(user_id=BOB)
            )
        with self.assertRaisesRegex(Refused, "REGISTERED_HOST_OWNER_DRIFT"):
            store.admit(binding, "input")
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(sa.select(sa.func.count()).select_from(operations)).scalar_one(), 0
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
