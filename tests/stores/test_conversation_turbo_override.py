"""Turbo shares the packed session-overrides blob with harness_override.

Regression: the Turbo write was inserted between ``if _unset_harness_override``
and its ``elif harness_override is not None`` branch, so any update that passed
``turbo`` (the create path passes both) silently dropped ``harness_override``.
"""

from __future__ import annotations

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize("turbo", [True, False])
def test_turbo_and_harness_override_are_both_written(
    conversation_store: SqlAlchemyConversationStore, turbo: bool
) -> None:
    conv = conversation_store.create_conversation()
    updated = conversation_store.update_conversation(conv.id, harness_override="pi", turbo=turbo)
    assert updated is not None
    assert updated.harness_override == "pi"
    assert updated.turbo is turbo
    reread = conversation_store.get_conversation(conv.id)
    assert reread is not None
    assert reread.harness_override == "pi"
    assert reread.turbo is turbo


def test_unset_harness_override_still_wins_with_turbo(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    conv = conversation_store.create_conversation()
    conversation_store.update_conversation(conv.id, harness_override="pi")
    updated = conversation_store.update_conversation(
        conv.id, _unset_harness_override=True, turbo=True
    )
    assert updated is not None
    assert updated.harness_override is None
    assert updated.turbo is True
