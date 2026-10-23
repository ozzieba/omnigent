"""Actual ASGI maintenance and ingress boundaries with private durable state."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse

from omnigent.db.db_models import SqlHost, workspace_scope
from omnigent.db.utils import _create_engine, make_named_managed_session_maker
from omnigent.errors import OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.native_coding_agents import NATIVE_CODING_AGENTS
from omnigent.server.auth import AuthProvider
from omnigent.server.host_admission import AsyncAdmission
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import hosts as host_routes
from omnigent.server.routes._sessions.orchestration import _is_native_terminal_session
from omnigent.server.routes.host_admission import create_host_admission_router
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.host_admission_store import Binding, Store, hosts, operations
from omnigent.stores.host_store import HostStore

HOST = "1" * 32
SESSION = "2" * 32
OWNER = "alice@example.test"
B = Binding(7, HOST, OWNER)


class FixtureAuth(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("x-test-subject")


class ConversationFixture:
    def __init__(self):
        self.conv = SimpleNamespace(
            id=SESSION,
            host_id=HOST,
            runner_id=None,
            labels={},
            title="synthetic",
            kind="default",
            harness_override="claude-sdk",
        )
        self.append_calls = 0

    def get_conversation(self, session_id):
        return self.conv if session_id == SESSION else None

    def append(self, *args, **kwargs):
        self.append_calls += 1
        raise AssertionError("PERSISTENCE_REACHED_BEFORE_DRAIN_GATE")


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="host-admission-asgi-")
        self.engine = _create_engine(f"sqlite:///{Path(self.directory.name) / 'private.db'}")
        for table in (SqlHost.__table__, hosts, operations):
            table.create(self.engine)
        with self.engine.begin() as conn:
            for workspace, owner in ((0, "bob@example.test"), (7, OWNER)):
                conn.execute(
                    SqlHost.__table__.insert().values(
                        workspace_id=workspace,
                        host_id=HOST,
                        user_id=owner,
                        name="synthetic",
                        status=1,
                        created_at=1,
                        updated_at=int(time.time()),
                    )
                )
        self.host_store = HostStore.__new__(HostStore)
        self.host_store._session = make_named_managed_session_maker(
            self.engine, query_name_prefix="test.host_read"
        )
        self.store = Store(self.engine)
        self.adapter = AsyncAdmission(self.store, timeout=2)
        self.ready = False
        self.conversations = ConversationFixture()
        self.app = FastAPI()
        self.app.state.host_admission = self.adapter
        self.auth = FixtureAuth()

        @self.app.exception_handler(OmnigentError)
        async def handle_omnigent_error(request, error):
            return JSONResponse({"code": error.code}, status_code=error.http_status)

        self.app.include_router(
            create_host_admission_router(
                adapter=self.adapter,
                auth_provider=self.auth,
                host_store=self.host_store,
                maintenance_ready=lambda: self.ready,
            ),
            prefix="/v1",
        )
        events = APIRouter()
        routes_events.register_events_routes(
            events,
            conversation_store=self.conversations,
            agent_store=SimpleNamespace(),
            auth_provider=self.auth,
        )
        self.app.include_router(events, prefix="/v1")
        registry = HostRegistry()
        with workspace_scope(7):
            registry.register(
                HOST,
                SimpleNamespace(),
                HostHelloFrame(
                    version="synthetic",
                    frame_protocol_version=1,
                    name="synthetic",
                    runners=[],
                ),
                OWNER,
            )
        self.app.include_router(
            host_routes.create_hosts_router(
                registry,
                self.host_store,
                self.conversations,
                auth_provider=self.auth,
            ),
            prefix="/v1",
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://synthetic.test",
            headers={"x-test-subject": OWNER},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.adapter.aclose()
        self.engine.dispose()
        self.directory.cleanup()

    async def request(self, method, path, **kwargs):
        with workspace_scope(7):
            return await self.client.request(method, path, **kwargs)

    def held(self):
        self.store.enroll(B)
        return self.store.drain(B, 0)

    def journal_states(self):
        with self.engine.connect() as conn:
            return list(conn.execute(sa.select(operations.c.state)).scalars())

    async def test_unqualified_or_nonboolean_readiness_cannot_enroll(self):
        for readiness in (False, 1, None):
            self.ready = readiness
            response = await self.request("POST", f"/v1/hosts/{HOST}/admission")
            self.assertEqual(response.status_code, 503, response.text)
        self.assertIsNone(self.store.binding_for_host(7, HOST))
        self.assertEqual(self.journal_states(), [])

    async def test_owner_and_workspace_come_from_auth_context(self):
        self.ready = True
        response = await self.request(
            "POST",
            f"/v1/hosts/{HOST}/admission",
            json={"owner": "bob@example.test", "workspace_id": 0},
            headers={"x-owner": "bob@example.test", "x-workspace": "0"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()["runtime_quiet_proved"])
        self.assertEqual(self.store.binding_for_host(7, HOST), B)
        self.assertIsNone(self.store.binding_for_host(0, HOST))

    async def test_authentication_and_wrong_owner_denied_without_mutation(self):
        self.ready = True
        for subject in ("", "local", "__public__", "bob@example.test"):
            response = await self.request(
                "POST", f"/v1/hosts/{HOST}/admission", headers={"x-test-subject": subject}
            )
            self.assertEqual(response.status_code, 403 if subject.startswith("bob") else 401)
        self.assertIsNone(self.store.binding_for_host(7, HOST))

    async def test_duplicate_enrollment_preserves_existing_fence(self):
        fence = self.held()
        self.ready = True
        response = await self.request("POST", f"/v1/hosts/{HOST}/admission")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.status(B)["fence_token"], fence.token)
        self.assertEqual(self.store.status(B)["generation"], 1)

    async def test_unqualified_readiness_cannot_begin_drain(self):
        self.store.enroll(B)
        response = await self.request(
            "POST", f"/v1/hosts/{HOST}/maintenance", json={"expected_generation": 0}
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.status(B)["state"], "open")

    async def test_strict_generation_and_scope_fields_rejected(self):
        self.ready = True
        self.store.enroll(B)
        bodies = [{"expected_generation": value} for value in (True, -1, "0", 0.1)]
        bodies.append({"expected_generation": 0, "owner": "bob@example.test"})
        for body in bodies:
            response = await self.request("POST", f"/v1/hosts/{HOST}/maintenance", json=body)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.status(B)["generation"], 0)

    async def test_drain_cas_and_owner_status_do_not_claim_quiet(self):
        self.ready = True
        self.store.enroll(B)
        response = await self.request(
            "POST", f"/v1/hosts/{HOST}/maintenance", json={"expected_generation": 0}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["runtime_quiet_proved"])
        token = response.json()["fence_token"]
        repeated = await self.request(
            "POST", f"/v1/hosts/{HOST}/maintenance", json={"expected_generation": 0}
        )
        self.assertEqual(repeated.status_code, 409)
        status = await self.request("GET", f"/v1/hosts/{HOST}/admission")
        self.assertEqual(status.json()["fence_token"], token)
        self.assertFalse(status.json()["runtime_quiet_proved"])
        denied = await self.request(
            "GET", f"/v1/hosts/{HOST}/admission", headers={"x-test-subject": "bob@example.test"}
        )
        self.assertEqual(denied.status_code, 403)

    async def test_hash_alone_has_no_release_endpoint(self):
        self.held()
        response = await self.request(
            "POST", f"/v1/hosts/{HOST}/maintenance/release", json={"verification_sha256": "a" * 64}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.status(B)["state"], "draining")

    async def test_real_events_handler_blocks_all_input_variants_before_effects(self):
        self.held()
        events = [
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "text", "text": "synthetic"}]},
            },
            {"type": "retry_session", "data": {}},
            {"type": "approval", "data": {}},
            {
                "type": "function_call_output",
                "data": {"call_id": "synthetic", "output": "synthetic"},
            },
        ]
        retry = AsyncMock(side_effect=AssertionError("RECOVERY_REACHED_BEFORE_GATE"))
        with patch.object(routes_events, "_retry_session_single_flight", retry):
            variants = (
                ({}, False),
                ({"omnigent.wrapper": NATIVE_CODING_AGENTS[0].wrapper_label}, True),
            )
            for labels, native in variants:
                self.conversations.conv.labels = labels
                self.assertEqual(_is_native_terminal_session(self.conversations.conv), native)
                for event in events:
                    response = await self.request(
                        "POST", f"/v1/sessions/{SESSION}/events", json=event
                    )
                    self.assertEqual(response.status_code, 409, response.text)
        retry.assert_not_awaited()
        self.assertEqual(self.conversations.append_calls, 0)
        self.assertEqual(self.journal_states(), [])

    async def test_existing_output_reporting_remains_available_during_drain(self):
        self.held()
        response = await self.request(
            "POST",
            f"/v1/sessions/{SESSION}/events",
            json={"type": "external_output_text_delta", "data": {"delta": "synthetic"}},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.journal_states(), [])

    async def test_unenrolled_scope_keeps_retry_behavior_without_auto_enrollment(self):
        retry = AsyncMock(return_value={"queued": False})
        with patch.object(routes_events, "_retry_session_single_flight", retry):
            response = await self.request(
                "POST",
                f"/v1/sessions/{SESSION}/events",
                json={"type": "retry_session", "data": {}},
            )
        self.assertEqual(response.status_code, 202, response.text)
        retry.assert_awaited_once()
        self.assertIsNone(self.store.binding_for_host(7, HOST))

    async def test_recovery_failure_after_admission_retains_unknown(self):
        self.store.enroll(B)
        retry = AsyncMock(side_effect=RuntimeError("synthetic uncertain recovery"))
        with patch.object(routes_events, "_retry_session_single_flight", retry):
            response = await self.request(
                "POST",
                f"/v1/sessions/{SESSION}/events",
                json={"type": "retry_session", "data": {}},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.journal_states(), [3])
        self.assertEqual(self.store.inspect(self.store.drain(B, 0))["unsettled_operations"], 1)

    async def test_real_launch_handler_refuses_before_worktree_bind_and_send(self):
        self.held()
        response = await self.request(
            "POST",
            f"/v1/hosts/{HOST}/runners",
            json={
                "session_id": SESSION,
                "workspace": "/synthetic",
                "git": {"branch_name": "synthetic"},
            },
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.journal_states(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
