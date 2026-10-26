"""Bounded, content-free summaries of failed-worker process logs."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_HEADER = re.compile(
    r"^(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\s+"
    r"(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+"
    r"([\w.]+)\s+([\w.]+)\s*\|\s*(.*)$"
)
_CATEGORIES = (
    ("connection", ("connect", "tunnel", "websocket")),
    ("startup", ("start", "spawn", "bootstrap", "ready")),
    ("failure", ("fail", "error", "exception", "timeout", "refused")),
    ("shutdown", ("stop", "exit", "disconnect")),
)


def preserve_process_log_summary(data_dir: Path, output: Path) -> None:
    """Save fixed log metadata and categories, never arbitrary message text."""
    lines: list[str] = []
    for role in ("host", "server", "cli"):
        directory = data_dir / "logs" / role
        if not directory.is_dir() or directory.is_symlink():
            continue
        files = sorted(directory.glob("*.log"), key=lambda p: p.name)[-12:]
        for path in files:
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        continue
                    file.seek(0, os.SEEK_END)
                    file.seek(max(0, file.tell() - 65536))
                    tail = file.read(65536).decode("utf-8", errors="replace")
            except OSError:
                continue
            # Do not emit file names, paths, URLs, messages, or traceback text.
            lines.append(f"[{role} process log]")
            for line in tail.splitlines()[-200:]:
                match = _HEADER.match(line)
                if match is None:
                    continue
                severity, timestamp, logger, function, message = match.groups()
                categories = [
                    name
                    for name, words in _CATEGORIES
                    if any(word in message.lower() for word in words)
                ]
                lines.append(
                    f"{severity} {timestamp} {logger} {function} "
                    f"categories={','.join(categories) or 'other'}"
                )
    if lines:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
