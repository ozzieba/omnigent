"""Real private SQLite transactions with controlled transfer boundaries."""

import asyncio
import io
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import sqlalchemy as sa

from omnigent.db.db_models import SqlHost, current_workspace_id, workspace_scope
from omnigent.db.enum_codecs import decode_host_operation_state
from omnigent.db.query_context import current_query_name
from omnigent.db.utils import _create_engine
from omnigent.server.host_admission import AsyncAdmission, guarded_operation
from omnigent.stores.host_admission_store import Binding, Refused, Store, hosts, operations

HERE = Path(__file__).resolve().parent
B = Binding(1, "1" * 32, "synthetic-owner")
PROOF = "a" * 64


class ObserveStore(Store):
    def __init__(self, engine, loop):
        super().__init__(engine)
        self.loop = loop
        self.entered = asyncio.Event()
        self.returned = asyncio.Event()
        self.release_admit = threading.Event()
        self.release_admit.set()
        self.release_unknown = threading.Event()
        self.release_unknown.set()
        self.unknown_entered = asyncio.Event()
        self.pause_after_commit = False
        self.fail_unknown = False
        self.fail_complete = False
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.thread_ids = set()

    def admit(self, binding, operation):
        with self.lock:
            self.active += 1
            self.max_active = max(self.active, self.max_active)
            self.thread_ids.add(threading.get_ident())
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            if not self.pause_after_commit and not self.release_admit.wait(2):
                raise TimeoutError("FIXTURE_ADMISSION_BOUND")
            ticket = super().admit(binding, operation)
            self.loop.call_soon_threadsafe(self.returned.set)
            if self.pause_after_commit and not self.release_admit.wait(2):
                raise TimeoutError("FIXTURE_TRANSFER_BOUND")
            return ticket
        finally:
            with self.lock:
                self.active -= 1

    def unknown(self, ticket):
        self.loop.call_soon_threadsafe(self.unknown_entered.set)
        if not self.release_unknown.wait(2):
            raise TimeoutError("FIXTURE_UNKNOWN_BOUND")
        if self.fail_unknown:
            raise RuntimeError("sensitive-fixture-error-must-not-be-logged")
        return super().unknown(ticket)

    def complete(self, ticket):
        if self.fail_complete:
            raise RuntimeError("FIXTURE_COMPLETE_FAILURE")
        return super().complete(ticket)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="async-gate-", dir=HERE)
        self.engine = _create_engine("sqlite:///" + str(Path(self.scratch.name) / "private.db"))
        for table in (SqlHost.__table__, hosts, operations):
            table.create(self.engine)
        self.register(B)
        self.store = ObserveStore(self.engine, asyncio.get_running_loop())
        self.store.enroll(B)
        self.adapter = AsyncAdmission(self.store, workers=2, timeout=0.5)
        self.invocations = 0

    def register(self, binding):
        with self.engine.begin() as conn:
            conn.execute(
                SqlHost.__table__.insert().values(
                    workspace_id=binding.workspace,
                    host_id=binding.host,
                    user_id=binding.owner,
                    name="synthetic-" + binding.host[:8],
                    status=1,
                    created_at=1,
                    updated_at=1,
                )
            )

    async def asyncTearDown(self):
        self.store.release_admit.set()
        self.store.release_unknown.set()
        self.adapter.timeout = 2
        await self.adapter.aclose()
        self.engine.dispose()
        self.scratch.cleanup()

    async def action(self):
        self.invocations += 1
        return "returned-action"

    def states(self):
        with self.engine.connect() as conn:
            return [
                decode_host_operation_state(code)
                for code in conn.execute(sa.select(operations.c.state)).scalars()
            ]

    async def test_database_lock_does_not_block_event_loop(self):
        blocker = self.engine.connect()
        blocker.exec_driver_sql("BEGIN IMMEDIATE")
        task = asyncio.create_task(guarded_operation(self.adapter, B, "input", self.action))
        try:
            await asyncio.wait_for(self.store.entered.wait(), 0.3)
            # The real admission is waiting for this connection's database lock.
            marker = asyncio.Event()
            asyncio.get_running_loop().call_soon(marker.set)
            await asyncio.wait_for(marker.wait(), 0.2)
            self.assertFalse(task.done())
            self.assertEqual(self.invocations, 0)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(await task, "returned-action")
        self.assertEqual(self.states(), ["complete"])
        self.assertNotIn(threading.get_ident(), self.store.thread_ids)

    async def test_cancel_after_real_commit_retains_late_unknown(self):
        self.store.pause_after_commit = True
        self.store.release_admit.clear()
        task = asyncio.create_task(guarded_operation(self.adapter, B, "launch", self.action))
        await asyncio.wait_for(self.store.returned.wait(), 0.3)
        self.assertEqual(self.states(), ["pending"])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.invocations, 0)
        self.store.release_admit.set()
        await self.adapter.aclose()
        self.assertEqual(self.states(), ["unknown"])
        fence = self.store.drain(B, 0)
        with self.assertRaisesRegex(Refused, "UNSETTLED"):
            self.store.release_after_verified(fence, PROOF)

    async def test_timeout_after_real_commit_retains_late_unknown(self):
        self.adapter.timeout = 0.06
        self.store.pause_after_commit = True
        self.store.release_admit.clear()
        task = asyncio.create_task(guarded_operation(self.adapter, B, "input", self.action))
        await asyncio.wait_for(self.store.returned.wait(), 0.3)
        with self.assertRaises(TimeoutError):
            await task
        self.assertEqual(self.states(), ["pending"])
        self.assertEqual(self.invocations, 0)
        self.store.release_admit.set()
        self.adapter.timeout = 1
        await self.adapter.aclose()
        self.assertEqual(self.states(), ["unknown"])

    async def test_cancel_waiting_for_worker_has_no_database_effect(self):
        await self.adapter.aclose()
        self.adapter = AsyncAdmission(self.store, workers=1, timeout=0.5)
        self.store.pause_after_commit = True
        self.store.release_admit.clear()
        first = asyncio.create_task(self.adapter.admit(B, "input"))
        await asyncio.wait_for(self.store.returned.wait(), 0.3)
        second = asyncio.create_task(guarded_operation(self.adapter, B, "launch", self.action))
        # Give the second coroutine a turn to reach the occupied worker bound.
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.store.release_admit.set()
        ticket = await first
        await self.adapter.complete(ticket)
        self.assertEqual(self.states(), ["complete"])
        self.assertEqual(self.invocations, 0)

    async def test_fixed_worker_bound_and_actions_complete(self):
        self.store.release_admit.clear()
        tasks = [
            asyncio.create_task(guarded_operation(self.adapter, B, "input", self.action))
            for _ in range(10)
        ]
        await asyncio.wait_for(self.store.entered.wait(), 0.3)
        self.store.release_admit.set()
        self.assertEqual(await asyncio.gather(*tasks), ["returned-action"] * 10)
        self.assertLessEqual(self.store.max_active, 2)
        self.assertLessEqual(len(self.store.thread_ids), 2)
        self.assertEqual(self.states(), ["complete"] * 10)

    async def test_draining_refuses_before_action(self):
        fence = await self.adapter.drain(B, 0)
        with self.assertRaisesRegex(Refused, "HOST_DRAINING"):
            await guarded_operation(self.adapter, B, "input", self.action)
        self.assertEqual(self.invocations, 0)
        self.assertEqual(self.states(), [])
        inspection = await self.adapter.inspect(fence)
        self.assertFalse(inspection["runtime_quiet_proved"])
        await self.adapter.release_after_verified(fence, PROOF)

    async def test_action_error_preserved_and_unknown_persists(self):
        error = ValueError("ORIGINAL_ACTION_ERROR")

        async def action():
            raise error

        with self.assertRaises(ValueError) as caught:
            await guarded_operation(self.adapter, B, "input", action)
        self.assertIs(caught.exception, error)
        await self.adapter.aclose()
        self.assertEqual(self.states(), ["unknown"])

    async def test_action_cancellation_preserved(self):
        entered = asyncio.Event()

        async def action():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(guarded_operation(self.adapter, B, "input", action))
        # Admission may use its full deadline before invoking the action.
        # The precondition wait must not cancel the test while admission is valid.
        await asyncio.wait_for(entered.wait(), self.adapter.timeout + 0.3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.adapter.aclose()
        self.assertEqual(self.states(), ["unknown"])

    async def test_repeated_cancel_keeps_classification_owned(self):
        entered = asyncio.Event()
        self.store.release_unknown.clear()

        async def action():
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(guarded_operation(self.adapter, B, "input", action))
        await asyncio.wait_for(entered.wait(), 0.3)
        task.cancel()
        await asyncio.wait_for(self.store.unknown_entered.wait(), 0.3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.states(), ["pending"])
        fence = self.store.drain(B, 0)
        with self.assertRaisesRegex(Refused, "UNSETTLED"):
            self.store.release_after_verified(fence, PROOF)
        self.store.release_unknown.set()
        await self.adapter.aclose()
        self.assertEqual(self.states(), ["unknown"])

    async def test_close_timeout_retains_custody_and_refuses_new_work(self):
        self.adapter.timeout = 0.06
        self.store.pause_after_commit = True
        self.store.release_admit.clear()
        task = asyncio.create_task(self.adapter.admit(B, "input"))
        await asyncio.wait_for(self.store.returned.wait(), 0.3)
        with self.assertRaises(TimeoutError):
            await task
        with self.assertRaises(TimeoutError):
            await self.adapter.aclose()
        with self.assertRaisesRegex(Refused, "CLOSING"):
            await self.adapter.admit(B, "launch")
        self.assertEqual(self.states(), ["pending"])
        self.store.release_admit.set()
        self.adapter.timeout = 1
        await self.adapter.aclose()
        self.assertEqual(self.states(), ["unknown"])

    async def test_unknown_failure_preserves_original_error_and_pending(self):
        self.store.fail_unknown = True
        error = ValueError("original-sensitive-action-detail")

        async def action():
            raise error

        sink = io.StringIO()
        with redirect_stderr(sink):
            with self.assertRaises(ValueError) as caught:
                await guarded_operation(self.adapter, B, "launch", action)
            await self.adapter.aclose()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.states(), ["pending"])
        self.assertEqual(sink.getvalue(), "HOST_ADMISSION_UNKNOWN_RETENTION_FAILED\n")

    async def test_completion_failure_does_not_claim_result_or_clear_pending(self):
        self.store.fail_complete = True
        with self.assertRaisesRegex(RuntimeError, "FIXTURE_COMPLETE_FAILURE"):
            await guarded_operation(self.adapter, B, "input", self.action)
        self.assertEqual(self.invocations, 1)
        self.assertEqual(self.states(), ["pending"])

    async def test_scope_owner_and_stale_generation_preserved(self):
        other = Binding(1, B.host, "wrong-owner")
        with self.assertRaisesRegex(Refused, "HOST_OWNER_DRIFT"):
            await self.adapter.admit(other, "launch")
        fence = await self.adapter.drain(B, 0)
        await self.adapter.release_after_verified(fence, PROOF)
        with self.assertRaisesRegex(Refused, "DRAIN_CAS_FAILED"):
            await self.adapter.drain(B, 0)
        self.assertEqual(self.states(), [])

    async def test_workspace_zero_and_negative_workspace_boundary(self):
        zero = Binding(0, "2" * 32, "synthetic-owner")
        self.register(zero)
        self.store.enroll(zero)
        ticket = await self.adapter.admit(zero, "launch")
        await self.adapter.complete(ticket)
        with self.assertRaisesRegex(Refused, "BAD_WORKSPACE"):
            Binding(-1, zero.host, zero.owner)

    async def test_workspace_and_query_context_reach_database_worker(self):
        observed = []

        @sa.event.listens_for(self.engine, "before_cursor_execute")
        def record(conn, cursor, statement, parameters, context, executemany):
            if threading.get_ident() != main_thread:
                observed.append((current_workspace_id(), current_query_name()))

        main_thread = threading.get_ident()
        bound = Binding(7, "3" * 32, "synthetic-owner")
        with workspace_scope(7):
            self.register(bound)
            self.store.enroll(bound)
            ticket = await self.adapter.admit(bound, "input")
            await self.adapter.complete(ticket)
        self.assertEqual(current_workspace_id(), 0)
        self.assertTrue(observed)
        self.assertEqual({workspace for workspace, name in observed}, {7})
        self.assertIn(
            "omnigent.host_admission_store.admit_host_operation",
            {name for workspace, name in observed},
        )
        self.assertIn(
            "omnigent.host_admission_store.settle_host_operation",
            {name for workspace, name in observed},
        )

    async def test_late_unknown_retains_original_workspace_context(self):
        observed = []

        @sa.event.listens_for(self.engine, "before_cursor_execute")
        def record(conn, cursor, statement, parameters, context, executemany):
            if current_query_name() == "omnigent.host_admission_store.settle_host_operation":
                observed.append(current_workspace_id())

        bound = Binding(9, "4" * 32, "synthetic-owner")
        self.store.pause_after_commit = True
        self.store.release_admit.clear()
        with workspace_scope(9):
            self.register(bound)
            self.store.enroll(bound)
            task = asyncio.create_task(self.adapter.admit(bound, "input"))
            await asyncio.wait_for(self.store.returned.wait(), 0.3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(current_workspace_id(), 0)
        self.store.release_admit.set()
        await self.adapter.aclose()
        self.assertTrue(observed)
        self.assertEqual(set(observed), {9})
        self.assertEqual(self.states(), ["unknown"])

    async def test_bad_bounds_refused_without_executor_creation(self):
        for n in (0, 9, True):
            with self.assertRaises(ValueError):
                AsyncAdmission(self.store, workers=n)
        for duration in (0, -1, 31, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                AsyncAdmission(self.store, timeout=duration)


if __name__ == "__main__":
    unittest.main(verbosity=2)
