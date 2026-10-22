"""Closed diagnostics for an owned REPL child that ended before readiness."""

from __future__ import annotations

import json
import os
import re

import pexpect

_OUTPUT_LIMIT = 16_384
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CLASS_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")
_CLASSES = frozenset(
    {
        "ModuleNotFoundError",
        "ImportError",
        "TypeError",
        "AttributeError",
        "ValueError",
        "RuntimeError",
        "OSError",
        "FileNotFoundError",
        "PermissionError",
        "TimeoutError",
        "ConnectionError",
        "OmnigentError",
    }
)


def describe_startup_eof(child: pexpect.spawn) -> str:
    """Return only typed status and closed markers; never child output or args.

    EOF need not mean the process exited. Pexpect's isalive may block after
    EOF, so inspect one owned-child wait result without blocking or reaping.
    """
    record: dict[str, object] = {
        "stage": "REPL_READINESS_EOF",
        "status_probe": "UNAVAILABLE",
        "exitstatus": None,
        "signalstatus": None,
        "observed_error_classes": [],
        "observed_error_markers": [],
    }
    try:
        if (
            isinstance(child, pexpect.spawn)
            and type(child.pid) is int
            and child.pid > 0
            and child.ptyproc.pid == child.pid
        ):
            cached = child.status
            if (
                child.terminated is True
                and child.ptyproc.terminated is True
                and type(cached) is int
                and child.ptyproc.status == cached
            ):
                if os.WIFEXITED(cached) and child.exitstatus == os.WEXITSTATUS(cached):
                    record["status_probe"] = "REAPED_CACHED_EXIT"
                    record["exitstatus"] = child.exitstatus
                elif os.WIFSIGNALED(cached) and child.signalstatus == os.WTERMSIG(cached):
                    record["status_probe"] = "REAPED_CACHED_SIGNAL"
                    record["signalstatus"] = child.signalstatus
            status = None
            if record["status_probe"] == "UNAVAILABLE":
                status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is None and record["status_probe"] == "UNAVAILABLE":
                record["status_probe"] = "NOT_EXITED_AT_PROBE"
            elif (
                status is not None
                and status.si_pid == child.pid
                and status.si_code == os.CLD_EXITED
            ):
                record["status_probe"] = "EXITED_NOT_REAPED"
                record["exitstatus"] = status.si_status
            elif (
                status is not None
                and status.si_pid == child.pid
                and status.si_code
                in (
                    os.CLD_KILLED,
                    os.CLD_DUMPED,
                )
            ):
                record["status_probe"] = "SIGNALED_NOT_REAPED"
                record["signalstatus"] = status.si_status
    except (AttributeError, OSError):
        pass

    try:
        before = child.before
        if isinstance(before, bytes):
            before = before[-_OUTPUT_LIMIT:].decode("utf-8", errors="replace")
        elif isinstance(before, str):
            before = before[-_OUTPUT_LIMIT:]
        else:
            before = ""
        classes: set[str] = set()
        markers: set[str] = set()
        for line in _ANSI.sub("", before).splitlines():
            line = line.strip()
            match = _CLASS_LINE.match(line)
            if match and match[1] in _CLASSES:
                classes.add(match[1])
            for marker, prefix in (
                ("CLICK_FORMATTED_ERROR", "Error: "),
                ("PYTHON_TRACEBACK", "Traceback (most recent call last):"),
                ("CLI_DETAILS_HINT", "Details logged to "),
                ("CLI_CRASH_REPORT", "A crash report was saved to:"),
            ):
                if line.startswith(prefix):
                    markers.add(marker)
        record["observed_error_classes"] = sorted(classes)
        record["observed_error_markers"] = sorted(markers)
    except Exception:
        record["output_classification"] = "UNAVAILABLE"

    return json.dumps(record, sort_keys=True)
