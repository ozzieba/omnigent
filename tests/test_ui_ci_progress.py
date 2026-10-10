"""Canonical progress must preserve phase behavior without logging parameters."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from tests.e2e_ui.ci_progress import UIProgress


class Item:
    def __init__(self, path, originalname="test_visible"):
        self.path = path
        self.originalname = originalname
        self.name = originalname + "[PRIVATE_PARAMETER_SENTINEL]"
        self.cls = None


@pytest.fixture
def progress_fixture(tmp_path, monkeypatch):
    path = tmp_path / "tests/e2e_ui/test_progress_probe.py"
    path.parent.mkdir(parents=True)
    path.write_text("def test_visible(): pass\n", encoding="utf-8")
    config = SimpleNamespace(
        rootpath=tmp_path,
        pluginmanager=SimpleNamespace(get_plugins=list),
    )
    output = io.StringIO()
    monkeypatch.setattr("sys.__stdout__", output)
    return UIProgress(config), Item(path), output


def records(output):
    prefix = "OMNIGENT_UI_PROGRESS "
    return [
        json.loads(line.removeprefix(prefix))
        for line in output.getvalue().splitlines()
        if line.startswith(prefix)
    ]


def test_collection_counts_all_selected_items_and_omits_parameters(progress_fixture):
    progress, first, output = progress_fixture
    second = Item(first.path)
    unbound = Item(first.path, originalname="test_not_declared")
    progress.pytest_collection_finish(SimpleNamespace(items=[first, second, unbound]))
    record = records(output)[0]
    assert record["selected_count"] == 3
    assert record["source_bound_count"] == 2
    assert record["unbound_count"] == 1
    assert progress._items[first][1:] == (1, 3)
    assert progress._items[second][1:] == (2, 3)
    assert "PRIVATE_PARAMETER_SENTINEL" not in output.getvalue()
    assert all("[" not in item[0] for item in progress._items.values())


def test_phase_emits_complete_start_before_work_and_elapsed_end(progress_fixture, monkeypatch):
    progress, item, output = progress_fixture
    progress.pytest_collection_finish(SimpleNamespace(items=[item]))
    clock = iter([10.0, 10.125])
    monkeypatch.setattr(
        "tests.e2e_ui.ci_progress.time",
        SimpleNamespace(monotonic=lambda: next(clock), time=lambda: 1.0),
    )
    phase = progress._phase(item, "call")
    assert next(phase) is None
    assert records(output)[-1]["event"] == "START"
    assert output.getvalue().endswith("\n")
    phase.close()
    end = records(output)[-1]
    assert end["event"] == "END"
    assert end["phase"] == "call"
    assert end["elapsed_seconds"] == 0.125
    assert "attempt" not in end


def test_phase_preserves_original_exception_without_logging_it(progress_fixture):
    progress, item, output = progress_fixture
    progress.pytest_collection_finish(SimpleNamespace(items=[item]))
    phase = progress._phase(item, "setup")
    next(phase)
    failure = RuntimeError("PRIVATE_FAILURE_SENTINEL")
    with pytest.raises(RuntimeError) as raised:
        phase.throw(failure)
    assert raised.value is failure
    assert records(output)[-1]["event"] == "END"
    assert "PRIVATE_FAILURE_SENTINEL" not in output.getvalue()


def test_unbound_phase_remains_silent(progress_fixture):
    progress, item, output = progress_fixture
    phase = progress._phase(item, "call")
    assert next(phase) is None
    phase.close()
    assert output.getvalue() == ""
