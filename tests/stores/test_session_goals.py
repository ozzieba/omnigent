"""Durability, concurrent ownership and lifecycle evidence."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import inspect

from omnigent.db.db_models import workspace_scope
from omnigent.entities.session_goal import SessionGoalConflict, SetSessionGoalRequest
from omnigent.stores.conversation_store import ConversationNotFoundError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def req(text="Verify same-snapshot hashes", rev=0, **kw):
    return SetSessionGoalRequest(objective=text, expected_revision=rev, **kw)


def test_reopen(db_uri):
    s = SqlAlchemyConversationStore(db_uri)
    sid = s.create_conversation().id
    g = s.set_session_goal(sid, req(), "owner")
    assert g.status == "paused"
    assert SqlAlchemyConversationStore(db_uri).get_session_goal(sid) == g
    assert s.get_conversation(sid).runner_id is None


@pytest.mark.parametrize("state", ["active", "paused", "blocked", "cancelled"])
def test_preserve_status(conversation_store, state):
    s = conversation_store
    sid = s.create_conversation().id
    s.set_session_goal(sid, req(status=state), "owner")
    g = s.set_session_goal(sid, req("Verify restore", 1), "owner")
    assert g.status == state
    assert g.revision == 2


def test_stale_update(conversation_store):
    s = conversation_store
    sid = s.create_conversation().id
    g = s.set_session_goal(sid, req(), "owner")
    with pytest.raises(SessionGoalConflict):
        s.set_session_goal(sid, req("Stale"), "other")
    assert s.get_session_goal(sid) == g


def test_concurrent_creation(db_uri):
    s = SqlAlchemyConversationStore(db_uri)
    sid = s.create_conversation().id

    def write(text):
        try:
            SqlAlchemyConversationStore(db_uri).set_session_goal(sid, req(text), "owner")
            return "stored"
        except SessionGoalConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ["Restore A", "Restore B"])) == ["conflict", "stored"]
    assert s.get_session_goal(sid).revision == 1


@pytest.mark.parametrize("initial_revision", [0, 1])
def test_concurrent_revision_after_waiting_for_lock(db_uri, initial_revision):
    """A writer that waits for the parent lock must read the winner's revision."""
    store = SqlAlchemyConversationStore(db_uri)
    if store._conv_engine.dialect.name != "mysql":
        pytest.skip("controlled REPEATABLE READ snapshot race requires MySQL")
    sid = store.create_conversation().id
    if initial_revision:
        store.set_session_goal(sid, req("Initial"), "owner")
    waiting = Event()
    release = Event()

    class DelayedWriter(SqlAlchemyConversationStore):
        def _lock_conversation(self, session, conversation_id):
            waiting.set()
            assert release.wait(10), "winner did not release the delayed writer"
            super()._lock_conversation(session, conversation_id)

    delayed = DelayedWriter(db_uri)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            delayed.set_session_goal, sid, req("Delayed", initial_revision), "owner"
        )
        try:
            assert waiting.wait(10), "delayed writer did not reach the lock"
            winner = store.set_session_goal(sid, req("Winner", initial_revision), "owner")
        finally:
            release.set()
        with pytest.raises(SessionGoalConflict):
            result.result(timeout=10)
    assert store.get_session_goal(sid) == winner


def test_completion_evidence(conversation_store):
    s = conversation_store
    sid = s.create_conversation().id
    with pytest.raises(ValueError, match="completion evidence"):
        s.set_session_goal(sid, req(status="completed"), "owner")
    assert s.get_session_goal(sid) is None
    g = s.set_session_goal(
        sid, req(status="completed", completion_evidence="Hashes matched"), "owner"
    )
    with pytest.raises(ValueError, match="explicit status"):
        s.set_session_goal(sid, req("Different snapshot", 1), "owner")
    assert s.get_session_goal(sid) == g


def test_tenants(conversation_store):
    s = conversation_store
    with workspace_scope(11):
        sid = s.create_conversation().id
        s.set_session_goal(sid, req(), "owner")
    with workspace_scope(12):
        assert s.get_session_goal(sid) is None
        with pytest.raises(ConversationNotFoundError):
            s.set_session_goal(sid, req(), "other")


def test_split_db(split_db_conversation_store):
    s = split_db_conversation_store
    sid = s.create_conversation().id
    g = s.set_session_goal(sid, req(), "owner")
    assert s.get_session_goal(sid) == g
    assert "session_goals" in inspect(s._conv_engine).get_table_names()


async def test_fork_delete(conversation_store):
    s = conversation_store
    sid = s.create_conversation().id
    g = s.set_session_goal(sid, req(), "owner")
    fork = s.fork_conversation(sid)
    assert s.get_session_goal(fork.id) is None
    assert s.get_session_goal(sid) == g
    await s.delete_conversation(sid)
    assert s.get_session_goal(sid) is None
