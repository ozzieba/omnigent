"""Opt-in, payload-free progress records for the UI CI shard."""

from __future__ import annotations

import ast
import contextlib
import json
import re
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest


class UIProgress:
    def __init__(self, config: pytest.Config) -> None:
        self._root = config.rootpath
        self._items: dict[pytest.Item, tuple[str, int, int]] = {}
        self._symbols: dict[Path, set[tuple[str | None, str]]] = {}
        try:
            pinned_reruns = version("pytest-rerunfailures") == "16.3"
        except PackageNotFoundError:
            pinned_reruns = False
        self._attempts = pinned_reruns and any(
            getattr(plugin, "__name__", None) == "pytest_rerunfailures"
            for plugin in config.pluginmanager.get_plugins()
        )

    def _canonical(self, item: pytest.Item) -> str | None:
        try:
            path = item.path.relative_to(self._root).as_posix()
        except ValueError:
            return None
        if not re.fullmatch(r"tests/e2e_ui/(?:\w+/)*test_\w+\.py", path, re.ASCII):
            return None
        name = getattr(item, "originalname", None)
        cls = getattr(item, "cls", None)
        owner = cls.__name__ if cls is not None else None
        if item.path not in self._symbols:
            try:
                module = ast.parse(item.path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeError):
                return None
            symbols = set()
            for node in module.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.add((None, node.name))
                elif isinstance(node, ast.ClassDef):
                    symbols.update(
                        (node.name, child.name)
                        for child in node.body
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    )
            self._symbols[item.path] = symbols
        if (owner, name) not in self._symbols[item.path]:
            return None
        symbol = f"{owner}::{name}" if owner is not None else name
        return f"{path}::{symbol}"

    @staticmethod
    def _write(record: dict[str, object]) -> None:
        # Bypass capture; complete LF records also flush a pending pytest prefix.
        sys.__stdout__.write("\nOMNIGENT_UI_PROGRESS " + json.dumps(record, sort_keys=True) + "\n")
        sys.__stdout__.flush()

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        count = len(session.items)
        for ordinal, item in enumerate(session.items, 1):
            canonical = self._canonical(item)
            if canonical is not None:
                self._items[item] = (canonical, ordinal, count)
        self._write(
            {
                "event": "COLLECTION",
                "timestamp": time.time(),
                "selected_count": count,
                "source_bound_count": len(self._items),
                "unbound_count": count - len(self._items),
            }
        )

    def _phase(self, item: pytest.Item, phase: str):
        bound = self._items.get(item)
        if bound is None:
            return (yield)
        canonical, ordinal, count = bound
        fields: dict[str, object] = {
            "canonical": canonical,
            "ordinal": ordinal,
            "selected_count": count,
            "phase": phase,
        }
        attempt = getattr(item, "execution_count", None)
        if phase != "item" and self._attempts and type(attempt) is int and attempt >= 1:
            fields["attempt"] = attempt
        started = time.monotonic()
        self._write({**fields, "event": "START", "timestamp": time.time()})
        try:
            return (yield)
        finally:
            self._write(
                {
                    **fields,
                    "event": "END",
                    "timestamp": time.time(),
                    "elapsed_seconds": time.monotonic() - started,
                }
            )

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        return (yield from self._phase(item, "item"))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        return (yield from self._phase(item, "setup"))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        return (yield from self._phase(item, "call"))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        return (yield from self._phase(item, "teardown"))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        bound = self._items.get(item)
        if bound is None or not report.failed or call.excinfo is None:
            return report
        canonical, ordinal, count = bound
        error_type = call.excinfo.type
        known = {AssertionError: "AssertionError", TimeoutError: "TimeoutError"}
        category = known.get(error_type, "OTHER")
        if (error_type.__module__, error_type.__name__) == (
            "playwright._impl._errors",
            "TimeoutError",
        ):
            category = "PLAYWRIGHT_TIMEOUT"
        elif (error_type.__module__, error_type.__name__) == ("_pytest.outcomes", "Failed"):
            category = "PYTEST_FAILED"
        record = {
            "event": "FAILURE",
            "timestamp": time.time(),
            "canonical": canonical,
            "ordinal": ordinal,
            "selected_count": count,
            "phase": report.when,
            "error_class": category,
        }
        for entry in call.excinfo.traceback[-64:]:
            if entry.path == item.path and entry.name == item.originalname:
                record["test_line"] = entry.lineno + 1
        with contextlib.suppress(OSError, ValueError):
            self._write(record)
        return report
