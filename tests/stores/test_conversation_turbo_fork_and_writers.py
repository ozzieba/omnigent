"""Turbo is default-off on forks and survives every other override writer."""

from __future__ import annotations

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def test_fork_does_not_carry_turbo(conversation_store: SqlAlchemyConversationStore) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.update_conversation(conv.id, turbo=True, model_override="gpt-5")
    fork = conversation_store.fork_conversation(conv.id)
    fork_id = fork.id if hasattr(fork, "id") else fork[0].id
    reread = conversation_store.get_conversation(fork_id)
    assert reread is not None
    assert reread.turbo is False  # default-off on forks (routing switches likewise not carried)
    assert conversation_store.get_conversation(conv.id).turbo is True


def test_other_override_writes_preserve_turbo(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.update_conversation(conv.id, turbo=True)
    for kw in (
        {"model_override": "gpt-5"},
        {"reasoning_effort": "high"},
        {"reported_model": "gpt-5-codex"},
        {"cost_control_mode_override": "on"},
        {"subagent_routing_override": "on"},
        {"harness_override": "pi"},
        {"_unset_model_override": True},
        {"_unset_reasoning_effort": True},
        {"_unset_cost_control_mode_override": True},
        {"_unset_subagent_routing_override": True},
        {"_unset_harness_override": True},
        {"title": "t"},
        {"archived": True},
    ):
        try:
            conversation_store.update_conversation(conv.id, **kw)
        except TypeError:
            continue
        got = conversation_store.get_conversation(conv.id)
        assert got is not None and got.turbo is True, kw
