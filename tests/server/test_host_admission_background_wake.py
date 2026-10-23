"""Private durable admission with actual background-wake effect boundaries."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import sqlalchemy as sa

from omnigent.db.db_models import SqlHost, workspace_scope
from omnigent.db.enum_codecs import decode_host_operation_state
from omnigent.db.utils import _create_engine
from omnigent.runtime import pending_elicitations
from omnigent.runtime import subagent_block_notifier as notifier
from omnigent.server import session_live_state
from omnigent.server.host_admission import AsyncAdmission
from omnigent.server.routes import sessions
from omnigent.stores.host_admission_store import Binding, Store, hosts, operations

HOST = "3" * 32
PARENT = "4" * 32
CHILD = "5" * 32
OWNER = "owner@example.test"
B = Binding(0, HOST, OWNER)


class BackgroundWakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="host-admission-background-")
        self.engine = _create_engine(f"sqlite:///{Path(self.directory.name) / 'private.db'}")
        for table in (SqlHost.__table__, hosts, operations):
            table.create(self.engine)
        with self.engine.begin() as conn:
            for workspace, owner in ((0, OWNER), (7, "other@example.test")):
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
        self.store = Store(self.engine)
        self.adapter = AsyncAdmission(self.store, timeout=2)
        self.parent = SimpleNamespace(id=PARENT, host_id=HOST, runner_id="synthetic")
        self.child = SimpleNamespace(
            id=CHILD, parent_conversation_id=PARENT, title="worker:synthetic"
        )
        self.conversations = SimpleNamespace(
            get_conversation=lambda selected: self.child if selected == CHILD else self.parent
        )
        self.forward = AsyncMock(return_value="confirmed")
        self.relay = Mock()
        self.runner = AsyncMock(return_value=object())
        # The actual function has its own module globals, independent of facade aliases.
        self.boundaries = patch.dict(
            sessions._wake_parent_for_blocked_child.__globals__,
            {
                "_get_runner_client": self.runner,
                "_ensure_runner_relay": self.relay,
                "_dispatch_session_event_to_runner": self.forward,
            },
        )
        self.boundaries.start()
        pending_elicitations.reset_for_tests()

    async def asyncTearDown(self):
        pending_elicitations.reset_for_tests()
        self.boundaries.stop()
        self.adapter.timeout = 2
        await self.adapter.aclose()
        self.engine.dispose()
        self.directory.cleanup()

    async def wake(self, adapter=True):
        return await asyncio.wait_for(
            sessions._wake_parent_for_blocked_child(
                PARENT,
                self.child,
                "public synthetic notice",
                conversation_store=self.conversations,
                runner_router=None,
                host_admission=self.adapter if adapter else None,
            ),
            3,
        )

    def rows(self):
        with self.engine.connect() as conn:
            return [
                (row.workspace_id, decode_host_operation_state(row.state))
                for row in conn.execute(sa.select(operations)).mappings()
            ]

    async def test_draining_refuses_before_relay_or_forward(self):
        self.store.enroll(B)
        self.store.drain(B, 0)
        self.assertIs(await self.wake(), False)
        self.forward.assert_not_awaited()
        self.relay.assert_not_called()
        self.assertEqual(self.rows(), [])

    async def test_open_host_dispatches_once_and_settles_transfer(self):
        self.store.enroll(B)
        self.assertIs(await self.wake(), True)
        self.assertEqual(self.forward.await_count, 1)
        self.assertEqual(self.relay.call_count, 1)
        self.assertEqual(self.rows(), [(0, "complete")])
        args = self.forward.await_args.args
        self.assertEqual(args[0], PARENT)
        self.assertEqual(args[2].data["content"][0]["text"], "public synthetic notice")
        self.assertFalse(self.store.status(B)["runtime_quiet_proved"])

    async def test_ambiguous_transport_is_unknown_and_not_retried(self):
        self.store.enroll(B)
        self.forward.side_effect = httpx.ConnectError("synthetic")

        async def dispatch(parent_id, child, notice):
            return await self.wake()

        owner = notifier.SubagentBlockNotifier(
            self.conversations, dispatch, asyncio.get_running_loop()
        )
        with patch.object(notifier, "_sleep", AsyncMock()):
            outcome = await asyncio.wait_for(
                owner._deliver_with_retry(PARENT, self.child, "synthetic", armed_id=None), 3
            )
        self.assertIs(outcome, notifier._WakeOutcome.UNKNOWN)
        self.assertEqual(self.forward.await_count, 1)
        self.assertEqual(self.rows(), [(0, "unknown")])
        fence = self.store.drain(B, 0)
        self.assertEqual(self.store.inspect(fence)["unsettled_operations"], 1)

    async def test_cancelled_forward_retains_unknown_without_repeat(self):
        self.store.enroll(B)
        entered = asyncio.Event()

        async def suspended(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.forward.side_effect = suspended
        task = asyncio.create_task(self.wake())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.forward.await_count, 1)
        self.assertEqual(self.rows(), [(0, "unknown")])

    async def test_trusted_workspace_is_used_for_background_binding(self):
        other = Binding(7, HOST, "other@example.test")
        self.store.enroll(other)
        self.store.drain(other, 0)
        with workspace_scope(7):
            self.assertIs(await self.wake(), False)
        self.forward.assert_not_awaited()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.status(other)["state"], "draining")

    async def test_owner_drift_refuses_without_reowning(self):
        self.store.enroll(B)
        with self.engine.begin() as conn:
            conn.execute(
                SqlHost.__table__.update()
                .where(
                    SqlHost.workspace_id == 0,
                    SqlHost.host_id == HOST,
                )
                .values(user_id="changed@example.test")
            )
        self.assertIs(await self.wake(), False)
        self.forward.assert_not_awaited()
        self.relay.assert_not_called()
        self.assertEqual(self.rows(), [])

    async def test_missing_runner_has_no_admission_ticket(self):
        self.store.enroll(B)
        self.runner.return_value = None
        self.assertIs(await self.wake(), False)
        self.assertEqual(self.rows(), [])
        self.forward.assert_not_awaited()

    async def test_unenrolled_host_preserves_legacy_behavior(self):
        self.assertIs(await self.wake(), True)
        self.assertEqual(self.forward.await_count, 1)
        self.assertEqual(self.rows(), [])

    async def test_late_admission_is_retained_and_never_resubmitted(self):
        self.store.enroll(B)
        self.adapter.timeout = 0.05
        release = threading.Event()
        calls = []
        original = self.store.admit

        def delayed(*args):
            calls.append(1)
            assert release.wait(2)
            return original(*args)

        async def dispatch(parent_id, child, notice):
            return await self.wake()

        owner = notifier.SubagentBlockNotifier(
            self.conversations, dispatch, asyncio.get_running_loop()
        )
        with (
            patch.object(self.store, "admit", delayed),
            patch.object(notifier, "_sleep", AsyncMock()),
        ):
            try:
                outcome = await owner._deliver_with_retry(
                    PARENT, self.child, "synthetic", armed_id=None
                )
                self.assertIs(outcome, notifier._WakeOutcome.UNKNOWN)
                self.assertEqual(calls, [1])
                self.forward.assert_not_awaited()
            finally:
                self.adapter.timeout = 2
                release.set()
            await self.adapter.aclose()
        self.assertEqual(self.rows(), [(0, "unknown")])

    async def test_configured_notifier_keeps_unknown_debounce_until_resolution(self):
        self.store.enroll(B)
        self.forward.side_effect = httpx.ConnectError("synthetic")
        original = notifier.SubagentBlockNotifier
        captured = []

        def construct(*args, **kwargs):
            result = original(*args, **kwargs)
            captured.append(result)
            return result

        async def finished(owner):
            while owner._inflight:
                await asyncio.sleep(0.005)

        with (
            patch.object(notifier, "SubagentBlockNotifier", construct),
            patch.object(notifier, "_escalation_sleep", AsyncMock()),
            patch.object(session_live_state, "_store", None),
        ):
            uninstall = sessions.configure_subagent_block_notifier(
                self.conversations, None, host_admission=self.adapter
            )
            try:
                event = {
                    "type": "response.elicitation_request",
                    "elicitation_id": "synthetic",
                    "params": {"mode": "form", "message": "synthetic"},
                }
                pending_elicitations.record_publish(CHILD, event)
                await asyncio.wait_for(finished(captured[0]), 3)
                self.assertIn("synthetic", captured[0]._notified)
                pending_elicitations.record_publish(CHILD, event)
                await asyncio.wait_for(finished(captured[0]), 3)
                self.assertEqual(self.forward.await_count, 1)
                self.assertEqual(self.rows(), [(0, "unknown")])
                pending_elicitations.record_publish(
                    CHILD,
                    {
                        "type": "response.elicitation_resolved",
                        "elicitation_id": "synthetic",
                        "action": "decline",
                    },
                )
                self.assertNotIn("synthetic", captured[0]._notified)
                self.assertEqual(self.forward.await_count, 1)
            finally:
                uninstall()
