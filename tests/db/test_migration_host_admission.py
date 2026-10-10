"""Actual Alembic upgrades and durable custody on private SQLite databases."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import OmnigentBase, SqlHost, SqlHostAdmission
from omnigent.db.utils import _build_alembic_config
from omnigent.stores.host_admission_store import Binding, Refused, Store, hosts, operations

HEAD = "gc1d2e3f4a5b"
PARENT = "zc1d2e3f4a5b"
B = Binding(0, "1" * 32, "alice@example.test")
PROOF = "a" * 64


def migrate(engine: sa.Engine, target: str, *, downgrade: bool = False) -> None:
    config = _build_alembic_config(str(engine.url))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        (command.downgrade if downgrade else command.upgrade)(config, target)


class MigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dir = tempfile.TemporaryDirectory(prefix="host-admission-migration-")
        cls.base_path = Path(cls.base_dir.name) / "full-chain.db"
        engine = sa.create_engine(f"sqlite:///{cls.base_path}")
        try:
            migrate(engine, "head")
            with engine.connect() as conn:
                assert (
                    conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
                    == HEAD
                )
        finally:
            engine.dispose()

    @classmethod
    def tearDownClass(cls):
        cls.base_dir.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="host-admission-case-")
        self.path = Path(self.directory.name) / "case.db"
        shutil.copyfile(self.base_path, self.path)
        self.engine = sa.create_engine(f"sqlite:///{self.path}")
        self.store = Store(self.engine)
        self.register(B)

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def register(self, binding):
        with self.engine.begin() as conn:
            conn.execute(
                SqlHost.__table__.insert().values(
                    workspace_id=binding.workspace,
                    host_id=binding.host,
                    user_id=binding.owner,
                    name="synthetic",
                    status=1,
                    created_at=1,
                    updated_at=1,
                )
            )

    def test_migration_matches_canonical_metadata_and_indexes(self):
        inspector = sa.inspect(self.engine)
        for table in (hosts, operations):
            self.assertIs(table.metadata, OmnigentBase.metadata)
            columns = {column["name"]: column for column in inspector.get_columns(table.name)}
            self.assertEqual(set(columns), set(table.c.keys()))
            self.assertEqual(
                inspector.get_pk_constraint(table.name)["constrained_columns"],
                [column.name for column in table.primary_key.columns],
            )
            self.assertEqual(inspector.get_foreign_keys(table.name), [])
            self.assertEqual(
                {c["name"] for c in inspector.get_check_constraints(table.name)},
                {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)},
            )
        self.assertEqual(
            inspector.get_indexes(operations.name)[0]["column_names"],
            ["workspace_id", "host_id", "state"],
        )

    def test_existing_hosts_are_not_automatically_enrolled(self):
        with self.assertRaisesRegex(Refused, "HOST_NOT_ENROLLED"):
            self.store.admit(B, "input")
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(sa.select(sa.func.count()).select_from(hosts)).scalar_one(), 0
            )

    def test_upgrade_from_maintained_head_preserves_existing_hosts(self):
        migrate(self.engine, PARENT, downgrade=True)
        with self.engine.connect() as conn:
            before = conn.execute(sa.select(SqlHost.__table__)).mappings().all()
            self.assertEqual(
                conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(),
                PARENT,
            )
        migrate(self.engine, "head")
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(sa.select(SqlHost.__table__)).mappings().all(), before)
            self.assertEqual(
                conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), HEAD
            )
            self.assertEqual(
                conn.execute(sa.select(sa.func.count()).select_from(hosts)).scalar_one(), 0
            )

    def test_workspace_default_and_binary_ids(self):
        with self.engine.begin() as conn:
            conn.execute(
                SqlHostAdmission.__table__.insert().values(
                    host_id=B.host,
                    owner_user_id=B.owner,
                    generation=0,
                    state=1,
                )
            )
        ticket = self.store.admit(B, "launch")
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.exec_driver_sql(
                    "SELECT workspace_id, length(host_id) FROM host_turnover_admission"
                ).one(),
                (0, 16),
            )
            self.assertEqual(
                conn.exec_driver_sql(
                    "SELECT length(token), length(host_id), operation, state "
                    "FROM host_turnover_operations"
                ).one(),
                (16, 16, 1, 1),
            )
        self.store.complete(ticket)

    def test_pending_unknown_and_fence_survive_engine_reopen(self):
        self.store.enroll(B)
        self.store.admit(B, "launch")
        unknown = self.store.admit(B, "input")
        self.store.unknown(unknown)
        fence = self.store.drain(B, 0)
        self.engine.dispose()
        self.engine = sa.create_engine(f"sqlite:///{self.path}")
        self.store = Store(self.engine)
        status = self.store.inspect(fence)
        self.assertEqual(status["unsettled_operations"], 2)
        self.assertFalse(status["runtime_quiet_proved"])
        with self.assertRaisesRegex(Refused, "UNSETTLED_ADMISSION_OPERATIONS"):
            self.store.release_after_verified(fence, PROOF)
        with self.assertRaisesRegex(Refused, "HOST_DRAINING"):
            self.store.admit(B, "launch")

    def test_registered_owner_drift_refuses_without_journal_mutation(self):
        self.store.enroll(B)
        with self.engine.begin() as conn:
            conn.execute(SqlHost.__table__.update().values(user_id="bob@example.test"))
        with self.assertRaisesRegex(Refused, "REGISTERED_HOST_OWNER_DRIFT"):
            self.store.admit(B, "input")
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(sa.select(sa.func.count()).select_from(operations)).scalar_one(), 0
            )

    def test_identical_host_ids_are_isolated_by_workspace(self):
        other = Binding(7, B.host, "bob@example.test")
        self.register(other)
        self.store.enroll(B)
        self.store.enroll(other)
        ticket = self.store.admit(other, "launch")
        with self.assertRaisesRegex(Refused, "REGISTERED_HOST_OWNER_DRIFT"):
            self.store.admit(Binding(7, B.host, B.owner), "input")
        self.store.complete(ticket)
        self.assertEqual(self.store.inspect(self.store.drain(B, 0))["unsettled_operations"], 0)

    def test_downgrade_refuses_populated_custody_even_when_complete(self):
        self.store.enroll(B)
        ticket = self.store.admit(B, "input")
        self.store.complete(ticket)
        with self.assertRaisesRegex(RuntimeError, "DOWNGRADE_REQUIRES_EMPTY_TABLES"):
            migrate(self.engine, PARENT, downgrade=True)
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), HEAD
            )
            self.assertEqual(conn.execute(sa.select(operations.c.state)).scalar_one(), 2)
            self.assertEqual(conn.execute(sa.select(hosts.c.generation)).scalar_one(), 0)

    def test_empty_downgrade_and_reupgrade(self):
        migrate(self.engine, PARENT, downgrade=True)
        self.assertNotIn(hosts.name, sa.inspect(self.engine).get_table_names())
        migrate(self.engine, "head")
        self.store.enroll(B)
        self.store.complete(self.store.admit(B, "launch"))

    def test_constraints_reject_invalid_fence_and_operation_codes(self):
        self.store.enroll(B)
        with self.assertRaises(sa.exc.IntegrityError), self.engine.begin() as conn:
            conn.execute(hosts.update().values(state=2, fence_token=None))
        with self.assertRaises(sa.exc.IntegrityError), self.engine.begin() as conn:
            conn.execute(
                operations.insert().values(
                    workspace_id=0,
                    token="2" * 32,
                    host_id=B.host,
                    owner_user_id=B.owner,
                    generation=0,
                    operation=99,
                    state=1,
                )
            )
        self.store.complete(self.store.admit(B, "launch"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
