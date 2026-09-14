"""Build receipts retain import-time evidence across disk replacement and fork."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent import build_receipt


def _isolated_package(tmp_path: Path, stamp: str | None) -> Path:
    package = tmp_path / "omnigent"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "build_receipt.py").write_bytes(Path(build_receipt.__file__).read_bytes())
    if stamp is not None:
        (package / "_build_info.py").write_text(stamp)
    return package


@pytest.mark.parametrize("inherited", [False, pytest.param(True, marks=pytest.mark.posix_only)])
def test_running_receipt_survives_installed_build_replacement(
    tmp_path: Path, inherited: bool
) -> None:
    """A retained interpreter (and its fork) reports A while a fresh one reports B."""
    package = _isolated_package(tmp_path, f"COMMIT_SHA = {'a' * 40!r}\nBUILD_TIME_EPOCH = 100\n")
    script = """
import json, os
from pathlib import Path
from omnigent.build_receipt import captured_build_receipt
first = captured_build_receipt().to_wire()
Path('omnigent/_build_info.py').write_text(
    "COMMIT_SHA = " + repr('b' * 40) + "\\nBUILD_TIME_EPOCH = 200\\n")
if INHERITED:
    child = os.fork()
    if child:
        _, status = os.waitpid(child, 0)
        raise SystemExit(os.waitstatus_to_exitcode(status))
print(json.dumps([first, captured_build_receipt().to_wire()]))
""".replace("INHERITED", repr(inherited))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=package.parent,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    first, retained = json.loads(result.stdout)
    assert first["commit_sha"] == retained["commit_sha"] == "a" * 40
    assert first["build_time_epoch"] == retained["build_time_epoch"] == 100
    assert first["captured_at_epoch"] == retained["captured_at_epoch"]
    assert first["capture_pid"] == retained["capture_pid"]
    assert retained["load_mode"] == ("inherited" if inherited else "process-import")
    assert (retained["process_pid"] != first["process_pid"]) is inherited
    fresh = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import json; from omnigent.build_receipt import captured_build_receipt; "
            "print(json.dumps(captured_build_receipt().to_wire()))",
        ],
        cwd=package.parent,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    replacement = json.loads(fresh.stdout)
    assert replacement["commit_sha"] == "b" * 40
    assert replacement["build_time_epoch"] == 200
    assert replacement["load_mode"] == "process-import"


@pytest.mark.parametrize(
    "stamp", [None, "COMMIT_SHA = ''\nBUILD_TIME_EPOCH = 100\n", "invalid syntax !"]
)
def test_missing_or_invalid_stamp_is_unknown(tmp_path: Path, stamp: str | None) -> None:
    package = _isolated_package(tmp_path, stamp)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import json; from omnigent.build_receipt import captured_build_receipt; "
            "print(json.dumps(captured_build_receipt().to_wire()))",
        ],
        cwd=package.parent,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["commit_sha"] is None
    assert receipt["build_time_epoch"] is None
    assert receipt["process_pid"] > 0


def test_receipt_round_trip_and_rejects_unrecognized_payloads() -> None:
    receipt = build_receipt.captured_build_receipt()
    wire = receipt.to_wire()
    assert build_receipt.BuildReceipt.from_wire(wire) == receipt
    assert wire["process_pid"] == os.getpid()
    assert build_receipt.BuildReceipt.from_wire({**wire, "schema_version": 2}) is None
    assert build_receipt.BuildReceipt.from_wire({**wire, "capture_pid": True}) is None
    assert (
        build_receipt.BuildReceipt.from_wire({**wire, "captured_at_epoch": float("nan")}) is None
    )
    assert (
        build_receipt.BuildReceipt.from_wire({**wire, "commit_sha": "unreviewed payload"}) is None
    )
    assert build_receipt.BuildReceipt.from_wire({**wire, "load_mode": "inherited"}) is None
    assert build_receipt.BuildReceipt.from_wire(None) is None
    decoded = build_receipt.BuildReceipt.from_wire({**wire, "ignored": "never reflected"})
    assert decoded is not None
    assert "ignored" not in decoded.to_wire()
