"""Import-time build stamps, not attestations of every loaded code object."""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, replace
from typing import Literal


def _valid_stamp(commit: object, epoch: object) -> bool:
    return (
        isinstance(commit, str)
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) is not None
        and type(epoch) is int
        and epoch >= 0
    )


@dataclass(frozen=True)
class BuildReceipt:
    """A captured build stamp and the process that originally imported it.

    ``captured_at_epoch`` is the snapshot time, not the OS process start time.
    A fork retains ``capture_pid``; ``process_pid`` identifies its reporter.
    """

    commit_sha: str | None
    build_time_epoch: int | None
    captured_at_epoch: float
    capture_pid: int
    process_pid: int
    load_mode: Literal["process-import", "inherited"]
    schema_version: int = 1

    def to_wire(self) -> dict[str, object]:
        """Serialize only the defined, non-secret receipt fields."""
        return asdict(self)

    @classmethod
    def from_wire(cls, value: object) -> BuildReceipt | None:
        """Ignore unsupported or malformed optional metadata without failing hello."""
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
            return None
        if value["schema_version"] != 1:
            return None
        commit, epoch = value.get("commit_sha"), value.get("build_time_epoch")
        if not ((commit is None and epoch is None) or _valid_stamp(commit, epoch)):
            return None
        captured = value.get("captured_at_epoch")
        capture_pid, process_pid = value.get("capture_pid"), value.get("process_pid")
        if not isinstance(captured, (int, float)) or isinstance(captured, bool):
            return None
        if not 0 <= captured <= 1e12:
            return None
        if type(capture_pid) is not int or type(process_pid) is not int:
            return None
        if capture_pid <= 0 or process_pid <= 0:
            return None
        mode = "process-import" if capture_pid == process_pid else "inherited"
        if value.get("load_mode") != mode:
            return None
        return cls(commit, epoch, float(captured), capture_pid, process_pid, mode)


def _capture() -> BuildReceipt:
    commit, epoch = None, None
    try:
        from omnigent import _build_info

        if _valid_stamp(_build_info.COMMIT_SHA, _build_info.BUILD_TIME_EPOCH):
            commit, epoch = _build_info.COMMIT_SHA, _build_info.BUILD_TIME_EPOCH
    except Exception:  # noqa: BLE001 — absent or mid-rewrite metadata is unknown
        pass
    pid = os.getpid()
    return BuildReceipt(commit, epoch, time.time(), pid, pid, "process-import")


# Imported before the runner graph; forks keep the original immutable snapshot.
_CAPTURED = _capture()


def captured_build_receipt() -> BuildReceipt:
    """Return retained evidence with the reporting PID, without reading files."""
    pid = os.getpid()
    return replace(
        _CAPTURED,
        process_pid=pid,
        load_mode="process-import" if pid == _CAPTURED.capture_pid else "inherited",
    )
