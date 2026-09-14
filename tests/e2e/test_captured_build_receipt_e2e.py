"""Isolated package artifact → runner subprocess → real tunnel → status.

This test uses a source-package archive and a fixture-managed runner transport,
not a fleet wheel build or a live host daemon. It imports the real entrypoint
but serves no sessions and starts no harnesses, providers, or MCP clients.
No LLM key is required. Every child receives an isolated allowlisted environment.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from omnigent.runner.identity import token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "omnigent"
_RUNNER_PROGRAM = """
import asyncio, sys
from omnigent.runner import _entry
from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
async def unused_app(scope, receive, send):
    raise AssertionError('this metadata fixture must receive no session requests')
asyncio.run(serve_tunnel(
    unused_app, server_url=sys.argv[1], runner_id=sys.argv[2],
    runner_version='build-receipt-fixture', tunnel_token=sys.argv[3],
))
"""


def _artifact(tmp_path: Path, commit: str, epoch: int) -> tuple[Path, bytes]:
    """Package real Python modules with a synthetic generated build stamp."""
    target = tmp_path / f"runtime-{epoch}.zip"
    stamp = f"COMMIT_SHA = {commit!r}\nBUILD_TIME_EPOCH = {epoch}\n".encode()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(_PACKAGE_ROOT.rglob("*.py")):
            if source.name != "_build_info.py":
                archive.write(source, str(source.relative_to(_PACKAGE_ROOT.parent)))
        archive.writestr("omnigent/_build_info.py", stamp)
    return target, stamp


@pytest.fixture
def receipt_server() -> Iterator[str]:
    """Serve the production tunnel and status routes over a private loopback socket."""
    app = FastAPI()
    app.include_router(create_runner_tunnel_router(TunnelRegistry()), prefix="/v1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                assert thread.is_alive(), "fixture server exited during startup"
                time.sleep(0.02)
            assert server.started, "fixture server did not start"
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "fixture server did not stop"


@contextlib.contextmanager
def _runner(
    installed: Path, state: Path, server_url: str, token: str
) -> Iterator[subprocess.Popen[bytes]]:
    """Own one transport-only child; import from the installed test artifact."""
    state.mkdir()
    env = {
        "PATH": os.defpath,
        "HOME": str(state),
        "XDG_CONFIG_HOME": str(state / "config"),
        "OMNIGENT_CONFIG_HOME": str(state / "omnigent-config"),
        "OMNIGENT_DATA_DIR": str(state / "omnigent-data"),
        "OMNIGENT_ANALYTICS": "0",
        "DISABLE_TELEMETRY": "true",
        "PYTHONPATH": str(installed),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with (state / "runner.log").open("wb") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                _RUNNER_PROGRAM,
                server_url,
                token_bound_runner_id(token),
                token,
            ],
            cwd=state,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _receipt(client: httpx.Client, token: str, proc: subprocess.Popen[bytes]) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert proc.poll() is None, "fixture runner exited; inspect its isolated runner.log"
        response = client.get(f"/v1/runners/{token_bound_runner_id(token)}/status")
        response.raise_for_status()
        status = response.json()
        if status["online"]:
            assert "captured_build" in status
            return status["captured_build"]
        time.sleep(0.05)
    raise AssertionError("fixture runner did not register within 30 seconds")


def test_installed_artifact_and_retained_runner_report_distinct_builds(
    tmp_path: Path, receipt_server: str
) -> None:
    """Status retains A after artifact B is installed; a fresh child reports B."""
    artifact_a, stamp_a = _artifact(tmp_path, "a" * 40, 100)
    artifact_b, stamp_b = _artifact(tmp_path, "b" * 40, 200)
    assert (
        hashlib.sha256(artifact_a.read_bytes()).digest()
        != hashlib.sha256(artifact_b.read_bytes()).digest()
    )
    installed = tmp_path / "installed"
    with zipfile.ZipFile(artifact_a) as archive:
        archive.extractall(installed)
    assert (installed / "omnigent/_build_info.py").read_bytes() == stamp_a
    with httpx.Client(base_url=receipt_server, trust_env=False, timeout=2) as client:
        with _runner(installed, tmp_path / "runner-a", receipt_server, "fixture-a") as old:
            first = _receipt(client, "fixture-a", old)
            assert first["commit_sha"] == "a" * 40
            assert first["build_time_epoch"] == 100
            assert first["process_pid"] == first["capture_pid"] == old.pid
            assert first["load_mode"] == "process-import"
            with zipfile.ZipFile(artifact_b) as archive:
                archive.extractall(installed)
            assert (installed / "omnigent/_build_info.py").read_bytes() == stamp_b
            assert _receipt(client, "fixture-a", old) == first
            with _runner(installed, tmp_path / "runner-b", receipt_server, "fixture-b") as new:
                replacement = _receipt(client, "fixture-b", new)
                assert replacement["commit_sha"] == "b" * 40
                assert replacement["build_time_epoch"] == 200
                assert replacement["process_pid"] == replacement["capture_pid"] == new.pid
                assert replacement["process_pid"] != first["process_pid"]
                assert _receipt(client, "fixture-a", old) == first
