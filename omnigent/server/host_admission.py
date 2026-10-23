"""Bounded async adapter for the unchanged persistent admission store.

The caller owns the engine and its connection/statement timeouts. A caller
deadline does not stop a database transaction: its future remains owned until
completion. Late admissions are classified UNKNOWN without invoking the action.
This component does not establish authorization, route coverage or runtime quiet.
"""

from __future__ import annotations

import asyncio
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from contextvars import copy_context

from omnigent.stores.host_admission_store import Refused


class AsyncAdmission:
    def __init__(self, store, *, workers=2, timeout=5.0):
        if type(workers) is not int or not 1 <= workers <= 8:
            raise ValueError("BAD_WORKER_BOUND")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 30
        ):
            raise ValueError("BAD_DATABASE_DEADLINE")
        self.store = store
        self.timeout = float(timeout)
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="host-admission"
        )
        self._slots = asyncio.BoundedSemaphore(workers)
        self._changed = asyncio.Event()
        self._loop = None
        self._inflight = set()
        self._retentions = set()
        self._closing = False
        self._closed = False

    def _bind_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise Refused("ADMISSION_EVENT_LOOP_DRIFT")
        return loop

    async def _submit(self, method, args, *, retention=False):
        loop = self._bind_loop()
        if self._closed or (self._closing and not retention):
            raise Refused("ADMISSION_ADAPTER_CLOSING")
        deadline = loop.time() + self.timeout
        await asyncio.wait_for(self._slots.acquire(), self.timeout)
        try:
            if self._closed or (self._closing and not retention):
                raise Refused("ADMISSION_ADAPTER_CLOSING")
            context = copy_context()
            future = loop.run_in_executor(self._executor, context.run, method, *args)
        except BaseException:
            self._slots.release()
            raise
        self._inflight.add(future)

        def finished(completed):
            self._inflight.remove(completed)
            self._slots.release()
            # Retrieve failures even when the requesting coroutine has left.
            if not completed.cancelled():
                completed.exception()
            self._changed.set()

        future.add_done_callback(finished)
        return future, deadline

    async def _wait(self, future, deadline):
        remaining = max(0.0, deadline - self._bind_loop().time())
        return await asyncio.wait_for(asyncio.shield(future), remaining)

    async def _call(self, method, *args, retention=False):
        future, deadline = await self._submit(method, args, retention=retention)
        return await self._wait(future, deadline)

    def retain_unknown(self, ticket):
        """Own classification after cancellation; pending already blocks drain."""
        self._bind_loop()
        task = asyncio.create_task(self._call(self.store.unknown, ticket, retention=True))
        self._retentions.add(task)

        def finished(completed):
            self._retentions.remove(completed)
            if completed.cancelled() or completed.exception() is not None:
                sys.stderr.write("HOST_ADMISSION_UNKNOWN_RETENTION_FAILED\n")
            self._changed.set()

        task.add_done_callback(finished)
        return task

    def _retain_late_admission(self, future):
        def finished(completed):
            if completed.cancelled():
                return
            if completed.exception() is not None:
                # No action ran. An uncertain commit can leave a durable pending
                # row, which remains unresolved rather than being auto-cleared.
                return
            ticket = completed.result()
            self.retain_unknown(ticket)

        future.add_done_callback(finished)

    async def admit(self, binding, operation):
        future = None
        try:
            future, deadline = await self._submit(self.store.admit, (binding, operation))
            return await self._wait(future, deadline)
        except BaseException:
            if future is not None:
                self._retain_late_admission(future)
            raise

    async def complete(self, ticket):
        return await self._call(self.store.complete, ticket)

    async def enroll(self, binding):
        return await self._call(self.store.enroll, binding)

    async def binding_for_host(self, workspace_id, host_id):
        return await self._call(self.store.binding_for_host, workspace_id, host_id)

    async def status(self, binding):
        return await self._call(self.store.status, binding)

    async def drain(self, binding, expected_generation):
        return await self._call(self.store.drain, binding, expected_generation)

    async def inspect(self, fence):
        return await self._call(self.store.inspect, fence)

    async def release_after_verified(self, fence, verification_sha256):
        return await self._call(self.store.release_after_verified, fence, verification_sha256)

    async def aclose(self):
        """Refuse new work; bounded wait for owned workers and classification.

        Timeout retains the executor and its futures. A later close can finish
        their custody; no database operation is replayed or cancelled by close.
        Dispose the external engine only after this method succeeds.
        """
        loop = self._bind_loop()
        if self._closed:
            return
        self._closing = True
        deadline = loop.time() + self.timeout
        # A just-completed future's late-admission callback runs before this
        # coroutine resumes from Event.wait(), and can add its owned retention.
        while True:
            self._changed.clear()
            if not self._inflight and not self._retentions:
                break
            await asyncio.wait_for(self._changed.wait(), max(0.0, deadline - loop.time()))
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._closed = True


async def guarded_operation(adapter, binding, operation, invoke, *, is_complete=None):
    ticket = await adapter.admit(binding, operation)
    try:
        result = await invoke()
        settled = is_complete is None or is_complete(result) is True
    except BaseException:
        retention = adapter.retain_unknown(ticket)
        # Repeated cancellation leaves classification owned by the adapter;
        # the pending ticket continues to block turnover.
        with suppress(BaseException):
            await asyncio.shield(retention)
        raise
    else:
        if settled:
            await adapter.complete(ticket)
        else:
            await asyncio.shield(adapter.retain_unknown(ticket))
        return result
