"""Unit tests for the runner-local sub-agent work registry guards."""

from __future__ import annotations

import asyncio
from typing import Any

from omnigent.runner import app as runner_app


def test_mark_terminal_with_mismatched_work_id_leaves_newer_dispatch_untouched() -> None:
    """
    A terminal report guarded by an older dispatch id must not touch a newer turn.

    Recovery paths await server reads between deciding a result and stamping
    it; a dispatch that replaced the entry meanwhile keeps its own slot.
    """
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    runner_app._session_inboxes_ref["conv_parent_guard"] = inbox
    try:
        entry = runner_app.register_subagent_work(
            parent_session_id="conv_parent_guard",
            child_session_id="conv_child_guard",
            agent="worker",
            title="guard",
            work_id="subagent_newer",
        )

        ack = runner_app.mark_subagent_work_terminal(
            "conv_child_guard",
            status="completed",
            output="result of the older dispatch",
            work_id="subagent_older",
        )

        assert ack.delivered is False
        assert ack.delivered_now is False
        assert ack.reason == runner_app._SUBAGENT_DELIVERY_DISPATCH_MISMATCH
        assert ack.entry is entry
        assert entry.status == "launching"
        assert entry.output is None
        assert entry.completed_at is None
        assert inbox.empty()

        matched = runner_app.mark_subagent_work_terminal(
            "conv_child_guard",
            status="completed",
            output="result of the newer dispatch",
            work_id="subagent_newer",
        )

        assert matched.delivered is True
        assert matched.delivered_now is True
        delivered = inbox.get_nowait()
        assert delivered["work_id"] == "subagent_newer"
        assert delivered["output"] == "result of the newer dispatch"
    finally:
        runner_app.unregister_subagent_work("conv_child_guard")
        runner_app._session_inboxes_ref.pop("conv_parent_guard", None)
