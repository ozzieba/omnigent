"""Whole-composer delivery regressions with a synthetic terminal only."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent import claude_native_bridge as bridge


def _pane(draft: str = "", footer: str = "? for shortcuts") -> str:
    return f"{'─' * 40}\n❯ {draft}\n{'─' * 40}\n{footer}\n"


@pytest.mark.parametrize(
    "draft",
    [
        "\n  resume the task",
        "first physical row\n  resume the task\n",
        "\n  [Pasted text #1 +5 lines]",
    ],
)
def test_draft_visibility_includes_continuation_rows(draft: str) -> None:
    assert bridge._draft_in_input_box(_pane(draft), "resume the task")


@pytest.mark.parametrize(
    "pane",
    [
        "❯ resume the task\n" + _pane(),
        "❯ [Pasted text #1 +5 lines]\n" + _pane(),
        "❯ resume the task\n",
        "─" * 40 + "\n❯ resume the task\n",
        _pane("resume the task", "search prompts: resume the task"),
        _pane("resume the task", "Do you want to run this command?"),
        _pane("resume the task", "Switch model?"),
        _pane("resume the task", "use this session only"),
        "Settings\n❯ resume the task\nEsc to cancel\n",
        "Settings\n" + _pane("resume the task", "Esc to cancel"),
    ],
)
def test_draft_visibility_rejects_transcript_and_unknown_surfaces(pane: str) -> None:
    assert not bridge._draft_in_input_box(pane, "resume the task")


def test_ordinary_approval_words_in_transcript_are_not_a_live_dialog() -> None:
    pane = "Do you want to continue?\n" + _pane("resume the task")
    assert bridge._draft_in_input_box(pane, "resume the task")


@pytest.fixture
def terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr(bridge, "_PASTE_COMMIT_TIMEOUT_S", 0.03)
    monkeypatch.setattr(bridge, "_INPUT_CLEAR_TIMEOUT_S", 0.03, raising=False)
    bridge_dir = tmp_path / "bridge"
    bridge.write_tmux_target(
        bridge_dir, socket_path=Path("/tmp/synthetic.sock"), tmux_target="main"
    )
    state = SimpleNamespace(
        pane=_pane(),
        calls=[],
        stashed=[],
        clear_works=True,
        paste_pane=_pane("\n  resume the task"),
        after_clear=None,
        after_paste=None,
        after_first_draft_capture=None,
        draft_captures=0,
        pasted=False,
    )

    def run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if "capture-pane" in cmd:
            if state.pasted:
                state.draft_captures += 1
                if state.draft_captures > 1 and state.after_first_draft_capture is not None:
                    state.pane = state.after_first_draft_capture
            return SimpleNamespace(returncode=0, stdout=state.pane, stderr="")
        state.calls.append(cmd)
        if cmd[-1] == "C-s":
            state.stashed.append(state.pane)
            if state.clear_works:
                state.pane = state.after_clear if state.after_clear is not None else _pane()
        if "paste-buffer" in cmd:
            state.pasted = True
            state.pane = state.after_paste if state.after_paste is not None else state.paste_pane
        if cmd[-1] == "Enter":
            state.pane = _pane()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", run)
    return bridge_dir, state


@pytest.mark.parametrize("footer", ["? for shortcuts", "esc to interrupt"])
def test_multiline_draft_is_stashed_once_before_pasting(terminal, footer: str) -> None:
    bridge_dir, state = terminal
    old = _pane("old first logical line\n  old second logical line\n  ", footer)
    state.pane = old
    bridge.inject_user_message(bridge_dir, content="\nresume the task")
    assert state.stashed == [old]
    commands = [call[3] for call in state.calls]
    assert commands == ["send-keys", "load-buffer", "paste-buffer", "send-keys"]
    assert state.calls[0][-1] == "C-s"
    assert state.calls[-1][-1] == "Enter"
    assert not any(key in call for call in state.calls for key in ("Escape", "C-a", "C-k"))


def test_empty_composer_never_toggles_the_stash(terminal) -> None:
    bridge_dir, state = terminal
    bridge.inject_user_message(bridge_dir, content="resume the task")
    assert state.stashed == []
    assert [call[3] for call in state.calls] == ["load-buffer", "paste-buffer", "send-keys"]


def test_failed_multiline_clear_never_pastes_or_submits(terminal) -> None:
    bridge_dir, state = terminal
    state.pane = _pane("old first line\n  old second line")
    state.clear_works = False
    with pytest.raises(RuntimeError, match="empty"):
        bridge.inject_user_message(bridge_dir, content="resume the task")
    assert len(state.stashed) == 1
    assert len(state.calls) == 1
    assert state.calls[0][-1] == "C-s"


@pytest.mark.parametrize(
    "after_clear",
    [
        "",
        "─" * 40 + "\n❯ \n",
        _pane("", "Do you want to run this command?"),
        "Settings\nEsc to cancel",
    ],
)
def test_uncertain_or_overlay_after_clear_never_pastes(terminal, after_clear: str) -> None:
    bridge_dir, state = terminal
    state.pane = _pane("old first line\n  old second line")
    state.after_clear = after_clear
    with pytest.raises(RuntimeError, match="empty"):
        bridge.inject_user_message(bridge_dir, content="resume the task")
    assert len(state.stashed) == 1
    assert [call[-1] for call in state.calls] == ["C-s"]


@pytest.mark.parametrize(
    "after_paste", ["", _pane("resume the task", "Do you want to run this command?")]
)
def test_unknown_or_approval_after_paste_never_gets_enter(terminal, after_paste: str) -> None:
    bridge_dir, state = terminal
    state.after_paste = after_paste
    with pytest.raises(RuntimeError, match="draft"):
        bridge.inject_user_message(bridge_dir, content="resume the task")
    assert not any(call[-1] == "Enter" for call in state.calls)


@pytest.mark.parametrize(
    "pane", ["", "❯ old draft", _pane("old draft", "Do you want to run this command?")]
)
def test_clear_sends_no_keys_to_unknown_or_approval_surface(terminal, pane: str) -> None:
    _bridge_dir, state = terminal
    state.pane = pane
    with pytest.raises(RuntimeError, match="empty"):
        bridge._clear_input_draft("/tmp/synthetic.sock", "main")
    assert state.calls == []


def test_approval_appearing_during_paste_settle_never_gets_enter(terminal) -> None:
    bridge_dir, state = terminal
    state.after_first_draft_capture = _pane("resume the task", "Do you want to run this command?")
    with pytest.raises(RuntimeError, match="draft"):
        bridge.inject_user_message(bridge_dir, content="resume the task")
    assert not any(call[-1] == "Enter" for call in state.calls)


def test_clear_waits_for_stable_empty_without_stashing_twice(terminal, monkeypatch) -> None:
    _bridge_dir, state = terminal
    old = _pane("old first line\n  old second line")
    frames = iter([old, old, _pane(), old, _pane(), _pane()])
    captures = []

    def capture(*_args):
        pane = next(frames)
        captures.append(pane)
        return pane

    monkeypatch.setattr(bridge, "_capture_pane", capture)
    bridge._clear_input_draft("/tmp/synthetic.sock", "main")
    assert len(captures) == 6
    assert [call[-1] for call in state.calls] == ["C-s"]


def test_one_occupied_repaint_followed_by_unknown_never_stashes(terminal, monkeypatch) -> None:
    _bridge_dir, state = terminal
    frames = iter([_pane("old draft")])
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_args: next(frames, ""))
    with pytest.raises(RuntimeError, match="empty"):
        bridge._clear_input_draft("/tmp/synthetic.sock", "main")
    assert state.calls == []
