"""Reuse journey first establishes the exact recorded daemon's online tunnel."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.omnigent import test_host_daemon_lifecycle_lock_e2e as journey


def test_offline_tunnel_is_not_a_reuse_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper waits past offline rather than interpreting a held record as ready."""
    record = tmp_path / "owned.json"
    record.write_text(json.dumps({"pid": 123, "host_id": "owned-host"}))
    observed = []
    responses = iter(["offline", "online"])

    def get(url, **kwargs):
        observed.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {"status": next(responses)})

    monkeypatch.setattr(journey.httpx, "get", get)
    monkeypatch.setattr(journey._POLL_PAUSE, "wait", lambda delay: None)
    journey._wait_for_daemon_online(record, 12345, 123)
    assert observed == ["http://127.0.0.1:12345/v1/hosts/owned-host"] * 2


def test_changed_record_refuses_before_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another daemon cannot satisfy the original process's precondition."""
    record = tmp_path / "owned.json"
    record.write_text(json.dumps({"pid": 999, "host_id": "another-host"}))

    def unexpected(*args, **kwargs):
        pytest.fail("changed identity must refuse before any HTTP request")

    monkeypatch.setattr(journey.httpx, "get", unexpected)
    with pytest.raises(AssertionError, match="daemon changed"):
        journey._wait_for_daemon_online(record, 12345, 123)
