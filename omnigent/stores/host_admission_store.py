"""Persistent admission and maintenance ownership for host operations.

The caller must obtain Binding from the authorized host/session resolver. This
store is not an authentication boundary. It serializes a drain with operation
admission on a durable row; completion does not attest inference/process quiet.
No DDL, connection creation, expiry, automatic release, or live-control entrypoint
is performed at import. PostgreSQL integration and whole-route coverage remain
separate acceptance gates.
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import cast

import sqlalchemy as sa

from omnigent.db.db_models import (
    OmnigentBase,
    SqlHost,
    SqlHostAdmission,
    SqlHostAdmissionOperation,
)
from omnigent.db.enum_codecs import (
    decode_host_admission_state,
    encode_host_admission_operation,
    encode_host_admission_state,
    encode_host_operation_state,
)
from omnigent.db.utils import make_named_managed_session_maker


class Refused(RuntimeError):
    pass


def require(ok, reason):
    if not ok:
        raise Refused(reason)


@dataclass(frozen=True)
class Binding:
    workspace: int
    host: str
    owner: str

    def __post_init__(self):
        require(type(self.workspace) is int and self.workspace >= 0, "BAD_WORKSPACE")
        require(
            isinstance(self.host, str) and re.fullmatch(r"[0-9a-f]{32}", self.host), "BAD_HOST"
        )
        require(
            isinstance(self.owner, str) and 0 < len(self.owner) <= 128,
            "AUTHENTICATED_OWNER_REQUIRED",
        )


@dataclass(frozen=True)
class Fence:
    binding: Binding
    generation: int
    token: str


@dataclass(frozen=True)
class Ticket:
    binding: Binding
    generation: int
    token: str
    operation: str


metadata = OmnigentBase.metadata
hosts = cast(sa.Table, SqlHostAdmission.__table__)
operations = cast(sa.Table, SqlHostAdmissionOperation.__table__)


class Store:
    def __init__(self, engine):
        require(engine.dialect.name in ("sqlite", "postgresql"), "UNSUPPORTED_DATABASE")
        self.engine = engine
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.host_admission_store", immediate=True
        )

    @contextmanager
    def _transaction(self, intent):
        with self._session(intent) as session:
            try:
                yield session.connection()
            except BaseException:
                session.rollback()
                raise

    @staticmethod
    def _key(binding):
        return sa.and_(hosts.c.workspace_id == binding.workspace, hosts.c.host_id == binding.host)

    def _registered_host(self, connection, binding):
        table = SqlHost.__table__
        query = sa.select(table.c.user_id).where(
            table.c.workspace_id == binding.workspace, table.c.host_id == binding.host
        )
        if self.engine.dialect.name == "postgresql":
            query = query.with_for_update()
        owner = connection.execute(query).scalar_one_or_none()
        require(owner is not None, "REGISTERED_HOST_REQUIRED")
        require(owner == binding.owner, "REGISTERED_HOST_OWNER_DRIFT")

    def _host(self, connection, binding):
        # Every mutation locks this same row before reading operations. On
        # SQLite BEGIN IMMEDIATE is the inter-connection write fence; on
        # PostgreSQL the row lock survives until commit.
        self._registered_host(connection, binding)
        query = sa.select(hosts).where(self._key(binding))
        if self.engine.dialect.name == "postgresql":
            query = query.with_for_update()
        row = connection.execute(query).mappings().one_or_none()
        require(row is not None, "HOST_NOT_ENROLLED")
        require(row["owner_user_id"] == binding.owner, "HOST_OWNER_DRIFT")
        return row

    @staticmethod
    def _unsettled(connection, binding):
        return connection.execute(
            sa.select(sa.func.count())
            .select_from(operations)
            .where(
                operations.c.workspace_id == binding.workspace,
                operations.c.host_id == binding.host,
                operations.c.state != encode_host_operation_state("complete"),
            )
        ).scalar_one()

    def enroll(self, binding):
        """Integration-only enrollment after real host-owner authorization."""
        with self._transaction("enroll_host_admission") as connection:
            self._registered_host(connection, binding)
            connection.execute(
                hosts.insert().values(
                    workspace_id=binding.workspace,
                    host_id=binding.host,
                    owner_user_id=binding.owner,
                    generation=0,
                    state=encode_host_admission_state("open"),
                    fence_token=None,
                    verification_sha256=None,
                )
            )

    def binding_for_host(self, workspace_id, host_id):
        """Resolve an explicitly enrolled scope without enrolling or reowning it."""
        with self._transaction("resolve_host_admission") as connection:
            table = SqlHost.__table__
            query = sa.select(table.c.user_id).where(
                table.c.workspace_id == workspace_id, table.c.host_id == host_id
            )
            if self.engine.dialect.name == "postgresql":
                query = query.with_for_update()
            owner = connection.execute(query).scalar_one_or_none()
            require(owner is not None, "REGISTERED_HOST_REQUIRED")
            binding = Binding(workspace_id, host_id, cast(str, owner))
            exists = connection.execute(
                sa.select(hosts.c.host_id).where(self._key(binding))
            ).scalar_one_or_none()
            if exists is None:
                return None
            self._host(connection, binding)
            return binding

    def status(self, binding):
        """Owner-checked status; zero operations does not prove runtime quiet."""
        with self._transaction("read_host_admission") as connection:
            row = self._host(connection, binding)
            return {
                "state": decode_host_admission_state(row["state"]),
                "generation": row["generation"],
                "fence_token": row["fence_token"],
                "unsettled_operations": self._unsettled(connection, binding),
                "runtime_quiet_proved": False,
            }

    def admit(self, binding, operation):
        require(operation in ("launch", "input"), "BAD_OPERATION")
        with self._transaction("admit_host_operation") as connection:
            row = self._host(connection, binding)
            require(row["state"] == encode_host_admission_state("open"), "HOST_DRAINING")
            ticket = Ticket(binding, row["generation"], uuid.uuid4().hex, operation)
            connection.execute(
                operations.insert().values(
                    token=ticket.token,
                    workspace_id=binding.workspace,
                    host_id=binding.host,
                    owner_user_id=binding.owner,
                    generation=ticket.generation,
                    operation=encode_host_admission_operation(operation),
                    state=encode_host_operation_state("pending"),
                )
            )
            return ticket

    def _settle(self, ticket, state):
        with self._transaction("settle_host_operation") as connection:
            self._host(connection, ticket.binding)
            result = connection.execute(
                operations.update()
                .where(
                    operations.c.token == ticket.token,
                    operations.c.workspace_id == ticket.binding.workspace,
                    operations.c.host_id == ticket.binding.host,
                    operations.c.owner_user_id == ticket.binding.owner,
                    operations.c.generation == ticket.generation,
                    operations.c.operation == encode_host_admission_operation(ticket.operation),
                    operations.c.state == encode_host_operation_state("pending"),
                )
                .values(state=encode_host_operation_state(state))
            )
            require(result.rowcount == 1, "TICKET_DRIFT_OR_ALREADY_SETTLED")

    def complete(self, ticket):
        """Known return/transfer boundary, not inference completion or quiet."""
        self._settle(ticket, "complete")

    def unknown(self, ticket):
        """Uncertain effect retains an unresolved durable ticket."""
        self._settle(ticket, "unknown")

    def drain(self, binding, expected_generation):
        require(type(expected_generation) is int and expected_generation >= 0, "BAD_GENERATION")
        with self._transaction("begin_host_maintenance") as connection:
            row = self._host(connection, binding)
            require(
                row["state"] == encode_host_admission_state("open")
                and row["generation"] == expected_generation,
                "DRAIN_CAS_FAILED",
            )
            fence = Fence(binding, row["generation"] + 1, uuid.uuid4().hex)
            connection.execute(
                hosts.update()
                .where(self._key(binding))
                .values(
                    state=encode_host_admission_state("draining"),
                    generation=fence.generation,
                    fence_token=fence.token,
                    verification_sha256=None,
                )
            )
            return fence

    def _owned(self, connection, fence):
        row = self._host(connection, fence.binding)
        require(
            row["state"] == encode_host_admission_state("draining")
            and row["generation"] == fence.generation
            and row["fence_token"] == fence.token,
            "FENCE_OWNERSHIP_DRIFT",
        )
        return row

    def inspect(self, fence):
        with self._transaction("inspect_host_maintenance") as connection:
            self._owned(connection, fence)
            return {
                "generation": fence.generation,
                "unsettled_operations": self._unsettled(connection, fence.binding),
                "admission_held": True,
                "runtime_quiet_proved": False,
            }

    def release_after_verified(self, fence, verification_sha256):
        """A receipt reference; caller must verify its actual postconditions."""
        require(
            isinstance(verification_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", verification_sha256),
            "BAD_VERIFICATION_REF",
        )
        with self._transaction("release_verified_maintenance") as connection:
            self._owned(connection, fence)
            require(
                self._unsettled(connection, fence.binding) == 0, "UNSETTLED_ADMISSION_OPERATIONS"
            )
            connection.execute(
                hosts.update()
                .where(self._key(fence.binding))
                .values(
                    state=encode_host_admission_state("open"),
                    fence_token=None,
                    verification_sha256=verification_sha256,
                )
            )
