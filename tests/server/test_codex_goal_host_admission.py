"""Real Codex goal routes with private admission and permission stores."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from fastapi import APIRouter

from omnigent.db.db_models import SqlHost, SqlSessionPermission, SqlUser
from omnigent.db.utils import make_named_managed_session_maker
from omnigent.harness_plugins import CODEX_NATIVE_CODING_AGENT
from omnigent.server.host_admission import guarded_operation
from omnigent.server.routes.codex import sessions as codex
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_spec = importlib.util.spec_from_file_location(
    "admission_asgi_fixture", Path(__file__).with_name("test_host_admission_api.py")
)
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
HOST, SESSION, OWNER, B = _fixture.HOST, _fixture.SESSION, _fixture.OWNER, _fixture.B
PATH = f"/v1/sessions/{SESSION}/codex_goal"


def result(status=200, payload='{"goal": null}'):
    return SimpleNamespace(status_code=status, body=payload)


class CodexAdmissionTests(unittest.IsolatedAsyncioTestCase):
    request = _fixture.ApiTests.request
    held = _fixture.ApiTests.held
    journal_states = _fixture.ApiTests.journal_states
    asyncTearDown = _fixture.ApiTests.asyncTearDown

    async def asyncSetUp(self):
        await _fixture.ApiTests.asyncSetUp(self)
        for table in (SqlUser.__table__, SqlSessionPermission.__table__):
            table.create(self.engine)
        with self.engine.begin() as conn:
            for user, level in ((OWNER, 4), ("reader@example.test", 1)):
                conn.execute(SqlUser.__table__.insert().values(workspace_id=7, id=user))
                conn.execute(
                    SqlSessionPermission.__table__.insert().values(
                        workspace_id=7, user_id=user, conversation_id=SESSION, level=level
                    )
                )
        permissions = SqlAlchemyPermissionStore.__new__(SqlAlchemyPermissionStore)
        permissions._engine = self.engine
        permissions._session = make_named_managed_session_maker(
            self.engine, query_name_prefix="test.codex_permission"
        )
        self.conversations.conv.parent_conversation_id = None
        self.conversations.conv.labels = {
            "omnigent.wrapper": CODEX_NATIVE_CODING_AGENT.wrapper_label
        }
        router = APIRouter()
        codex.register_codex_session_routes(
            router,
            conversation_store=self.conversations,
            runner_router=None,
            auth_provider=self.auth,
            permission_store=permissions,
            runner_exit_reports=None,
        )
        self.app.include_router(router, prefix="/v1")

    async def test_all_goal_mutations_refused_before_live_forward_or_launch(self):
        self.held()
        forward = AsyncMock(side_effect=AssertionError("FORWARD_BEFORE_GATE"))
        launch = AsyncMock(side_effect=AssertionError("LAUNCH_BEFORE_GATE"))
        with (
            patch.object(codex, "_forward_session_change_to_runner", forward),
            patch.object(codex, "_launch_runner_for_codex_goal", launch),
        ):
            cases = (
                ("PUT", PATH, {"objective": "synthetic"}),
                ("PATCH", PATH + "/status", {"status": "active"}),
                ("PATCH", PATH + "/status", {"status": "paused"}),
                ("DELETE", PATH, None),
            )
            for method, path, body in cases:
                response = await self.request(method, path, json=body)
                self.assertEqual(response.status_code, 409, response.text)
        forward.assert_not_awaited()
        launch.assert_not_awaited()
        self.assertEqual(self.journal_states(), [])

    async def test_goal_read_available_during_drain_and_cannot_wake_runner(self):
        self.held()
        forward = AsyncMock(return_value=None)
        launch = AsyncMock(side_effect=AssertionError("READ_WOKE_RUNNER"))
        with (
            patch.object(codex, "_forward_session_change_to_runner", forward),
            patch.object(codex, "_launch_runner_for_codex_goal", launch),
        ):
            response = await self.request("GET", PATH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"goal": None})
        self.assertEqual(forward.await_args.args[2], {"type": "goal_get"})
        launch.assert_not_awaited()
        self.assertEqual(self.journal_states(), [])

    async def test_open_mutation_has_durable_pending_ticket_before_forward(self):
        self.store.enroll(B)

        async def forward(*args):
            self.assertEqual(self.journal_states(), [1])
            return result()

        with patch.object(codex, "_forward_session_change_to_runner", forward):
            response = await self.request("PUT", PATH, json={"objective": "synthetic"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.journal_states(), [2])

    async def test_unenrolled_scope_retains_behavior_without_enrollment(self):
        forward = AsyncMock(return_value=result())
        with patch.object(codex, "_forward_session_change_to_runner", forward):
            response = await self.request("PUT", PATH, json={"objective": "synthetic"})
        self.assertEqual(response.status_code, 200, response.text)
        forward.assert_awaited_once()
        self.assertIsNone(self.store.binding_for_host(7, HOST))
        self.assertEqual(self.journal_states(), [])

    async def test_absent_adapter_preserves_existing_route(self):
        del self.app.state.host_admission
        forward = AsyncMock(return_value=result())
        with patch.object(codex, "_forward_session_change_to_runner", forward):
            response = await self.request("PUT", PATH, json={"objective": "synthetic"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.journal_states(), [])

    async def test_missing_runner_result_retained_unknown_without_replay(self):
        self.store.enroll(B)
        forward = AsyncMock(return_value=None)
        launch = AsyncMock(return_value=None)
        with (
            patch.object(codex, "_forward_session_change_to_runner", forward),
            patch.object(codex, "_launch_runner_for_codex_goal", launch),
        ):
            response = await self.request("PUT", PATH, json={"objective": "synthetic"})
        self.assertEqual(response.status_code, 503, response.text)
        forward.assert_awaited_once()
        launch.assert_awaited_once()
        self.assertEqual(self.journal_states(), [3])
        self.assertEqual(self.store.inspect(self.store.drain(B, 0))["unsettled_operations"], 1)

    async def test_non_success_runner_response_retained_unknown(self):
        self.store.enroll(B)
        forward = AsyncMock(return_value=result(503, '{"error": "synthetic", "detail": "closed"}'))
        launch = AsyncMock(side_effect=AssertionError("REPLAY_AFTER_RESPONSE"))
        with (
            patch.object(codex, "_forward_session_change_to_runner", forward),
            patch.object(codex, "_launch_runner_for_codex_goal", launch),
        ):
            response = await self.request("PUT", PATH, json={"objective": "synthetic"})
        self.assertEqual(response.status_code, 503, response.text)
        forward.assert_awaited_once()
        launch.assert_not_awaited()
        self.assertEqual(self.journal_states(), [3])

    async def test_forward_exception_retained_unknown(self):
        self.store.enroll(B)
        forward = AsyncMock(side_effect=RuntimeError("synthetic transport failure"))
        with patch.object(codex, "_forward_session_change_to_runner", forward):
            response = await self.request("PUT", PATH, json={"objective": "synthetic"})
        self.assertEqual(response.status_code, 500)
        forward.assert_awaited_once()
        self.assertEqual(self.journal_states(), [3])

    async def test_real_permission_store_precedes_admission_and_forward(self):
        self.held()
        forward = AsyncMock(return_value=result())
        with patch.object(codex, "_forward_session_change_to_runner", forward):
            denied = await self.request(
                "PUT",
                PATH,
                json={"objective": "synthetic"},
                headers={"x-test-subject": "reader@example.test"},
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            forward.assert_not_awaited()
            read = await self.request(
                "GET", PATH, headers={"x-test-subject": "reader@example.test"}
            )
            self.assertEqual(read.status_code, 200, read.text)
            self.client.headers.pop("x-test-subject")
            unauthenticated = await self.request("PUT", PATH, json={"objective": "synthetic"})
            self.assertEqual(unauthenticated.status_code, 401, unauthenticated.text)
        self.assertEqual(self.journal_states(), [])

    async def test_owner_drift_and_forged_workspace_header_do_not_bypass_fence(self):
        self.held()
        forward = AsyncMock(side_effect=AssertionError("FORWARD_BEFORE_GATE"))
        with patch.object(codex, "_forward_session_change_to_runner", forward):
            forged = await self.request(
                "PUT", PATH, json={"objective": "synthetic"}, headers={"x-workspace": "0"}
            )
            self.assertEqual(forged.status_code, 409, forged.text)
            with self.engine.begin() as conn:
                conn.execute(
                    sa.update(SqlHost).where(SqlHost.workspace_id == 7).values(user_id="bob")
                )
            drift = await self.request("PUT", PATH, json={"objective": "synthetic"})
            self.assertEqual(drift.status_code, 409, drift.text)
        forward.assert_not_awaited()
        self.assertEqual(self.journal_states(), [])

    async def test_nonboolean_completion_predicate_retains_unknown(self):
        self.store.enroll(B)
        returned = await guarded_operation(
            self.adapter,
            B,
            "input",
            AsyncMock(return_value="synthetic"),
            is_complete=lambda value: 1,
        )
        self.assertEqual(returned, "synthetic")
        self.assertEqual(self.journal_states(), [3])

    async def test_completion_predicate_failure_retains_unknown(self):
        self.store.enroll(B)

        def classify(value):
            raise ValueError("synthetic classification failure")

        with self.assertRaises(ValueError):
            await guarded_operation(
                self.adapter, B, "input", AsyncMock(return_value="synthetic"), is_complete=classify
            )
        self.assertEqual(self.journal_states(), [3])
