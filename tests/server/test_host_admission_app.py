"""Real app-factory wiring with private stores and controlled runner transport."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import sqlalchemy as sa

from omnigent.db.utils import _engine_cache, _engine_lock
from omnigent.harness_plugins import CODEX_NATIVE_CODING_AGENT
from omnigent.host.frames import HostHelloFrame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as server_app
from omnigent.server.auth import AuthProvider
from omnigent.server.host_admission import AsyncAdmission
from omnigent.server.host_admission_integration import HostAdmissionIntegration
from omnigent.server.routes.codex import sessions as codex
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_admission_store import Binding, Refused, Store, operations
from omnigent.stores.host_store import HostStore

HOST = "3" * 32
SESSION = "4" * 32
OWNER = "owner@example.test"
B = Binding(0, HOST, OWNER)


class FixtureAuth(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("x-test-subject")


class AppAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="host-admission-app-")
        root = Path(self.directory.name)
        self.uri = f"sqlite:///{root / 'private.db'}"
        self.host_store = HostStore(self.uri)
        self.engine = self.host_store._engine
        self.store = Store(self.engine)
        self.adapter = AsyncAdmission(self.store, timeout=2)
        self.host_store.upsert_on_connect(HOST, "synthetic", OWNER)
        self.conversations = SqlAlchemyConversationStore(self.uri)
        artifact_store = LocalArtifactStore(str(root / "artifacts"))
        self.kwargs = {
            "agent_store": SqlAlchemyAgentStore(self.uri),
            "conversation_store": self.conversations,
            "file_store": SqlAlchemyFileStore(self.uri),
            "artifact_store": artifact_store,
            "agent_cache": AgentCache(artifact_store=artifact_store, cache_dir=root / "cache"),
            "host_store": self.host_store,
            "auth_provider": FixtureAuth(),
            "server_config": {},
        }
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        await self.adapter.aclose()
        with _engine_lock:
            self.assertIs(_engine_cache.pop(self.uri), self.engine)
        self.engine.dispose()
        self.directory.cleanup()

    def app(self, **extra):
        args = {**self.kwargs, **extra}
        return server_app.create_app(**args)

    def client(self, app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://synthetic.test",
            headers={"x-test-subject": OWNER},
        )
        self.clients.append(client)
        return client

    async def test_default_factory_does_not_mount_or_enroll_admission(self):
        app = self.app()
        self.assertFalse(hasattr(app.state, "host_admission"))
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertNotIn("/v1/hosts/{host_id}/admission", paths)
        self.assertIsNone(self.store.binding_for_host(0, HOST))
        response = await self.client(app).get("/health")
        self.assertEqual(response.status_code, 200, response.text)

    async def test_configuration_cannot_enable_default_readiness(self):
        app = self.app(
            host_admission=self.adapter,
            server_config={"host_maintenance_ready": True, "host_admission_ready": True},
        )
        self.assertIs(app.state.host_admission, self.adapter)
        response = await self.client(app).post(f"/v1/hosts/{HOST}/admission")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIsNone(self.store.binding_for_host(0, HOST))

    async def test_invalid_injection_rejected_before_factory_effects(self):
        cases = (
            {"host_maintenance_ready": lambda: True},
            {"host_admission": self.adapter, "auth_provider": None},
            {"host_admission": self.adapter, "host_store": None},
            {"host_admission": self.adapter, "host_store": SimpleNamespace(_engine=None)},
            {"host_admission": self.adapter, "host_maintenance_ready": True},
            {"host_admission": SimpleNamespace()},
        )
        with patch.object(server_app, "ServerMcpPool", side_effect=AssertionError("EARLY_EFFECT")):
            for extra in cases:
                with self.assertRaises(ValueError):
                    self.app(**extra)
        self.assertIsNone(self.store.binding_for_host(0, HOST))

    async def test_factory_mounts_owner_api_with_explicit_trusted_readiness(self):
        app = self.app(host_admission=self.adapter, host_maintenance_ready=lambda: True)
        client = self.client(app)
        denied = await client.post(
            f"/v1/hosts/{HOST}/admission", headers={"x-test-subject": "other@example.test"}
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        enrolled = await client.post(f"/v1/hosts/{HOST}/admission")
        self.assertEqual(enrolled.status_code, 201, enrolled.text)
        self.assertFalse(enrolled.json()["runtime_quiet_proved"])
        wrong = await client.post(
            f"/v1/hosts/{HOST}/maintenance", json={"expected_generation": True}
        )
        self.assertEqual(wrong.status_code, 422, wrong.text)
        drained = await client.post(
            f"/v1/hosts/{HOST}/maintenance", json={"expected_generation": 0}
        )
        self.assertEqual(drained.status_code, 200, drained.text)
        self.assertEqual(drained.json()["generation"], 1)
        self.assertFalse(drained.json()["runtime_quiet_proved"])
        status = await client.get(f"/v1/hosts/{HOST}/admission")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["state"], "draining")
        missing = await client.post(f"/v1/hosts/{HOST}/admission/release")
        self.assertEqual(missing.status_code, 404, missing.text)

    async def test_real_factory_codex_guard_uses_the_same_adapter(self):
        self.store.enroll(B)
        self.store.drain(B, 0)
        app = self.app(host_admission=self.adapter)
        client = self.client(app)
        conv = SimpleNamespace(
            id=SESSION,
            host_id=HOST,
            parent_conversation_id=None,
            labels={"omnigent.wrapper": CODEX_NATIVE_CODING_AGENT.wrapper_label},
            title="synthetic",
            kind="default",
        )
        forward = AsyncMock(side_effect=AssertionError("DRAIN_BYPASS"))
        launch = AsyncMock(side_effect=AssertionError("DRAIN_BYPASS"))
        with (
            patch.object(self.conversations, "get_conversation", return_value=conv),
            patch.object(codex, "_forward_session_change_to_runner", forward),
            patch.object(codex, "_launch_runner_for_codex_goal", launch),
        ):
            response = await client.put(
                f"/v1/sessions/{SESSION}/codex_goal", json={"objective": "synthetic"}
            )
        self.assertEqual(response.status_code, 409, response.text)
        forward.assert_not_awaited()
        launch.assert_not_awaited()
        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(sa.select(sa.func.count()).select_from(operations)).scalar_one(), 0
            )

    async def test_real_factory_startup_failure_still_closes_adapter(self):
        app = self.app(host_admission=self.adapter)
        manager = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("synthetic startup")))
        with (
            patch.object(server_app, "HarnessProcessManager", return_value=manager),
            patch("omnigent.telemetry.init_client"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic startup"):
                async with app.router.lifespan_context(app):
                    self.fail("FAILED_STARTUP_ENTERED_APP")
        with self.assertRaisesRegex(Refused, "ADMISSION_ADAPTER_CLOSING"):
            await self.adapter.status(B)
        with self.engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT 1").scalar_one(), 1)

    async def test_real_factory_events_share_fence_and_keep_output_reporting(self):
        self.store.enroll(B)
        self.store.drain(B, 0)
        app = self.app(host_admission=self.adapter)
        client = self.client(app)
        conv = SimpleNamespace(
            id=SESSION,
            host_id=HOST,
            runner_id=None,
            labels={},
            title="synthetic",
            kind="default",
            harness_override="claude-sdk",
        )
        retry = AsyncMock(side_effect=AssertionError("RETRY_BEFORE_GATE"))
        with (
            patch.object(self.conversations, "get_conversation", return_value=conv),
            patch.object(
                self.conversations, "append", side_effect=AssertionError("WRITE_BEFORE_GATE")
            ),
            patch.object(routes_events, "_retry_session_single_flight", retry),
        ):
            for event in (
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
            ):
                response = await client.post(f"/v1/sessions/{SESSION}/events", json=event)
                self.assertEqual(response.status_code, 409, response.text)
            reporting = await client.post(
                f"/v1/sessions/{SESSION}/events",
                json={"type": "external_output_text_delta", "data": {"delta": "synthetic"}},
            )
            self.assertEqual(reporting.status_code, 202, reporting.text)
        retry.assert_not_awaited()

    async def test_real_factory_launch_shares_fence_before_workspace_effects(self):
        self.store.enroll(B)
        self.store.drain(B, 0)
        app = self.app(host_admission=self.adapter)
        app.state.host_registry.register(
            HOST,
            SimpleNamespace(),
            HostHelloFrame(
                version="synthetic", frame_protocol_version=1, name="synthetic", runners=[]
            ),
            OWNER,
        )
        conv = SimpleNamespace(
            id=SESSION,
            host_id=HOST,
            runner_id=None,
            agent_id=None,
            labels={},
            title="synthetic",
            kind="default",
            harness_override="claude-sdk",
        )
        with (
            patch.object(self.conversations, "get_conversation", return_value=conv),
            patch.object(
                self.conversations, "set_host_id", side_effect=AssertionError("BIND_BEFORE_GATE")
            ),
        ):
            response = await self.client(app).post(
                f"/v1/hosts/{HOST}/runners",
                json={
                    "session_id": SESSION,
                    "workspace": "/synthetic",
                    "git": {"branch_name": "synthetic"},
                },
            )
        self.assertEqual(response.status_code, 409, response.text)

    async def test_close_timeout_retains_worker_and_late_unknown_without_retry(self):
        self.store.enroll(B)
        self.adapter.timeout = 0.08
        entered = threading.Event()
        release = threading.Event()
        calls = []
        original = self.store.admit

        def blocked(binding, operation):
            calls.append(operation)
            entered.set()
            if not release.wait(3):
                raise AssertionError("FIXTURE_RELEASE_MISSING")
            return original(binding, operation)

        integration = HostAdmissionIntegration(
            adapter=self.adapter,
            host_store=self.host_store,
            auth_provider=self.kwargs["auth_provider"],
            maintenance_ready=None,
        )
        try:
            with patch.object(self.store, "admit", blocked):
                with self.assertRaises(TimeoutError):
                    await self.adapter.admit(B, "input")
                self.assertTrue(entered.is_set())
                with self.assertRaises(TimeoutError):
                    async with integration.lifespan():
                        pass
                with self.engine.connect() as conn:
                    self.assertEqual(conn.exec_driver_sql("SELECT 1").scalar_one(), 1)
        finally:
            release.set()
            self.adapter.timeout = 2
            await self.adapter.aclose()
        self.assertEqual(calls, ["input"])
        with self.engine.connect() as conn:
            self.assertEqual(list(conn.execute(sa.select(operations.c.state)).scalars()), [3])
        self.assertEqual(self.store.inspect(self.store.drain(B, 0))["unsettled_operations"], 1)
