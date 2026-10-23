"""Public declared-model metadata through real HTTP, without starting a turn."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml


@pytest.fixture(autouse=True)
def _enforce_min_server_version() -> None:
    """This fixture starts the branch interpreter itself, never a compat server."""


@pytest.fixture
def metadata_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Own the two mock-only subprocesses; leave parent/shared-suite state untouched."""
    from tests.e2e.conftest import find_free_port

    repository = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    environment = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "PYTHONPATH": str(repository),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "OPENAI_API_KEY": "mock-key",
        "OMNIGENT_AUTH_ENABLED": "0",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    mock_url = f"http://127.0.0.1:{find_free_port()}"
    server_url = f"http://127.0.0.1:{find_free_port()}"
    builtin = tmp_path / "metadata-builtin"
    builtin.mkdir()
    (builtin / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "metadata-builtin",
                "executor": {
                    "type": "omnigent",
                    "model": "synthetic-builtin-model",
                    "config": {"harness": "openai-agents"},
                    "auth": {
                        "type": "api_key",
                        "api_key": "mock-key",
                        "base_url": f"{mock_url}/v1",
                    },
                },
                "instructions": "Synthetic metadata only. No turn is submitted.",
            }
        )
    )
    server_config = tmp_path / "server.yaml"
    server_config.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "synthetic-policy-model",
                    "connection": {"base_url": f"{mock_url}/v1", "api_key": "mock-key"},
                }
            }
        )
    )
    environment["OPENAI_BASE_URL"] = f"{mock_url}/v1"
    environment["OMNIGENT_BUILTIN_AGENT_DIRS"] = str(builtin)
    processes: list[subprocess.Popen[bytes]] = []
    with (tmp_path / "subprocesses.log").open("wb") as log:
        try:
            for arguments, url in (
                (
                    [
                        sys.executable,
                        str(repository / "tests/server/integration/mock_llm_server.py"),
                        mock_url.rsplit(":", 1)[1],
                    ],
                    f"{mock_url}/stats",
                ),
                (
                    [
                        sys.executable,
                        "-m",
                        "omnigent.cli",
                        "server",
                        "--host",
                        "127.0.0.1",
                        "--no-open",
                        "--port",
                        server_url.rsplit(":", 1)[1],
                        "--database-uri",
                        f"sqlite:///{tmp_path / 'metadata.db'}",
                        "--artifact-location",
                        str(tmp_path / "artifacts"),
                        "--config",
                        str(server_config),
                    ],
                    f"{server_url}/health",
                ),
            ):
                process = subprocess.Popen(
                    arguments,
                    env=environment,
                    cwd=repository,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(process)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Owned metadata subprocess exited during startup")
                    try:
                        if httpx.get(url, timeout=1).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Owned metadata subprocess did not become healthy")
            yield server_url, mock_url
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def test_declared_models_are_visible_without_inference(metadata_server: tuple[str, str]) -> None:
    """Exercise catalog and uploaded-session serialization across real subprocesses."""
    live_server, mock_llm_server_url = metadata_server
    with httpx.Client(base_url=live_server, timeout=15) as client:
        catalog = client.get("/v1/agents")
        catalog.raise_for_status()
        builtin = next(
            agent for agent in catalog.json()["data"] if agent["name"] == "metadata-builtin"
        )
        assert builtin["model_selection"] == {
            "declared_model": "synthetic-builtin-model",
            "source_field": "parsed_agent_spec.executor.model",
            "selection_authority": "DECLARED_ONLY",
            "resolution": "EXPLICIT_DECLARED",
            "served_model": None,
        }
        synthetic_model = "synthetic-metadata-model"
        config = {
            "spec_version": 1,
            "name": "metadata-intent-only",
            "executor": {
                "type": "omnigent",
                "model": synthetic_model,
                "config": {"harness": "openai-agents"},
                "auth": {
                    "type": "api_key",
                    "api_key": "mock-key",
                    "base_url": f"{mock_llm_server_url}/v1",
                },
            },
            "instructions": "Synthetic metadata fixture. No turn is submitted.",
        }
        bundle = io.BytesIO()
        with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
            data = yaml.safe_dump(config).encode()
            member = tarfile.TarInfo("config.yaml")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        created = client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({})},
            files={"bundle": ("agent.tar.gz", bundle.getvalue(), "application/gzip")},
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        uploaded = client.get(f"/v1/sessions/{session_id}/agent")
        uploaded.raise_for_status()
        assert uploaded.json()["model_selection"] == {
            "declared_model": synthetic_model,
            "source_field": "parsed_agent_spec.executor.model",
            "selection_authority": "DECLARED_ONLY",
            "resolution": "EXPLICIT_DECLARED",
            "served_model": None,
        }
        # Neither discovery nor uploading a bundle is evidence of a served model.
        stats = httpx.get(f"{mock_llm_server_url}/stats", timeout=5)
        stats.raise_for_status()
        assert stats.json()["request_count"] == 0
