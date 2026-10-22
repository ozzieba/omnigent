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


def failure_call(item, error):
    source = "def test_visible():\n    raise error\n"
    item.path.write_text(source)
    namespace = {"error": error}
    exec(compile(source, str(item.path), "exec"), namespace)
    try:
        namespace["test_visible"]()
    except Exception:
        return SimpleNamespace(excinfo=pytest.ExceptionInfo.from_current())
    raise AssertionError("fixture did not fail")


def report_failure(progress, item, call):
    report = SimpleNamespace(failed=True, when="call")
    hook = progress.pytest_runtest_makereport(item, call)
    next(hook)
    with pytest.raises(StopIteration) as stopped:
        hook.send(report)
    assert stopped.value.value is report


def test_failed_report_emits_closed_class_line_and_preserves_report(progress_fixture):
    progress, item, output = progress_fixture
    progress.pytest_collection_finish(SimpleNamespace(items=[item]))
    report_failure(progress, item, failure_call(item, AssertionError("PRIVATE_FAILURE_SENTINEL")))
    record = records(output)[-1]
    assert record["event"] == "FAILURE"
    assert record["canonical"] == "tests/e2e_ui/test_progress_probe.py::test_visible"
    assert record["error_class"] == "AssertionError"
    assert record["test_line"] == 2
    assert "PRIVATE_FAILURE_SENTINEL" not in output.getvalue()
    assert "PRIVATE_PARAMETER_SENTINEL" not in output.getvalue()


def test_unknown_exception_name_and_message_are_not_exported(progress_fixture):
    progress, item, output = progress_fixture
    progress.pytest_collection_finish(SimpleNamespace(items=[item]))
    private_type = type("PRIVATE_TYPE_SENTINEL", (RuntimeError,), {})
    report_failure(progress, item, failure_call(item, private_type("PRIVATE_MESSAGE_SENTINEL")))
    assert records(output)[-1]["error_class"] == "OTHER"
    assert "PRIVATE_TYPE_SENTINEL" not in output.getvalue()
    assert "PRIVATE_MESSAGE_SENTINEL" not in output.getvalue()


def test_failed_report_sink_failure_preserves_report(progress_fixture, monkeypatch):
    progress, item, _ = progress_fixture
    progress.pytest_collection_finish(SimpleNamespace(items=[item]))
    call = failure_call(item, TimeoutError("PRIVATE_FAILURE_SENTINEL"))

    def fail_write(record):
        raise OSError("closed diagnostic sink")

    monkeypatch.setattr(progress, "_write", fail_write)
    report_failure(progress, item, call)


def test_failed_report_unbound_item_is_silent(progress_fixture):
    progress, item, output = progress_fixture
    report_failure(progress, item, failure_call(item, AssertionError("PRIVATE")))
    assert output.getvalue() == ""


def test_real_pytest_failure_is_emitted_before_later_blocked_test(tmp_path):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    root = Path(__file__).parents[1]
    tests = tmp_path / "tests/e2e_ui"
    tests.mkdir(parents=True)
    marker = tmp_path / "later-started"
    (tmp_path / "conftest.py").write_text(
        "from tests.e2e_ui.ci_progress import UIProgress\n"
        "def pytest_configure(config):\n"
        "    config.pluginmanager.register(UIProgress(config))\n"
    )
    (tests / "test_failure_probe.py").write_text(
        "def test_first_failure():\n"
        "    assert False, 'PRIVATE_FAILURE_SENTINEL'\n"
        "def test_later_block():\n"
        "    import time\n"
        "    from pathlib import Path\n"
        f"    Path({str(marker)!r}).write_text('started')\n"
        "    time.sleep(60)\n"
    )
    output_path = tmp_path / "private-pytest-output"
    with output_path.open("w") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "pytest", "--rootdir", str(tmp_path), "-q", str(tests)],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(root), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.exists(), "later test never reached its blocking boundary"
            assert child.poll() is None
            failures = [
                r for r in records(io.StringIO(output_path.read_text())) if r["event"] == "FAILURE"
            ]
            assert len(failures) == 1
            assert failures[0]["error_class"] == "AssertionError"
            assert failures[0]["test_line"] == 2
            assert "PRIVATE_FAILURE_SENTINEL" not in json.dumps(failures)
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
