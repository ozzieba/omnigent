"""Tests for the llmq-backed OpenCode catalog enrichment."""

from __future__ import annotations

import json
import urllib.error

import pytest

from omnigent import opencode_llmq_catalog as catalog


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Reset the module cache and point the disk cache at a temp dir."""
    monkeypatch.setattr(catalog, "_cache", {"fetched_at": 0.0, "models": []})
    monkeypatch.setattr(catalog, "_DISK_CACHE", tmp_path / "cache.json")
    yield


def _llmq_rows() -> list[dict]:
    return [
        {"id": "opencode-go/glm-5.3-flash", "bare_id": "glm-5.3-flash", "pricing": {
            "input_per_million_usd": 0.2,
            "cached_input_per_million_usd": 0.05,
            "output_per_million_usd": 1.0,
        }},
        {"id": "opencode-go/minimax-m3", "bare_id": "minimax-m3", "pricing": None},
    ]


def test_merge_enriches_matching_cli_row_and_appends_llmq_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "llmq_opencode_models", lambda force=False: _llmq_rows())
    cli_rows = [
        {
            "id": "opencode-go/glm-5.3-flash",
            "model": "glm-5.3-flash",
            "providerID": "opencode-go",
            "displayName": "opencode-go/glm-5.3-flash",
        },
        {"id": "opencode/big-pickle", "model": "big-pickle", "providerID": "opencode"},
    ]
    merged = catalog.merge_llmq_pricing(cli_rows)
    assert merged[0]["id"] == "opencode-go/glm-5.3-flash"
    assert merged[0]["pricing"]["output_per_million_usd"] == 1.0
    # CLI row not known to llmq passes through unpriced, in position.
    assert merged[1]["id"] == "opencode/big-pickle"
    assert "pricing" not in merged[1]
    # llmq-only row appended, marked, priced exactly as llmq reported.
    assert merged[2]["id"] == "opencode-go/minimax-m3"
    assert merged[2]["source"] == "llmq"
    assert merged[2]["pricing"] is None


def test_merge_matches_by_provider_and_model_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "llmq_opencode_models", lambda force=False: _llmq_rows())
    cli_rows = [
        {
            "id": "weird-spelling/glm-5.3-flash",
            "model": "glm-5.3-flash",
            "providerID": "opencode-go",
        },
    ]
    merged = catalog.merge_llmq_pricing(cli_rows)
    # The provider/model pair matched, so the row is priced (and not duplicated).
    assert merged[0]["pricing"]["input_per_million_usd"] == 0.2
    assert [r["id"] for r in merged].count("opencode-go/glm-5.3-flash") == 1


def test_merge_degrades_to_unpriced_cli_rows_on_llmq_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(force: bool = False) -> list[dict]:
        raise RuntimeError("gateway down")

    monkeypatch.setattr(catalog, "llmq_opencode_models", _boom)
    cli_rows = [{"id": "opencode/big-pickle", "model": "big-pickle"}]
    assert catalog.merge_llmq_pricing(cli_rows) == cli_rows


def test_llmq_opencode_models_uses_disk_cache_when_gateway_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"fetched_at": 1.0, "models": _llmq_rows()}))
    monkeypatch.setattr(catalog, "_DISK_CACHE", cache_file)

    def _fail(*a, **k):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(catalog, "_fetch_llmq_models", _fail)
    rows = catalog.llmq_opencode_models(force=True)
    assert rows[0]["id"] == "opencode-go/glm-5.3-flash"


def test_price_map_reads_vendored_table(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    table = {
        "window_fractions": {"five_hour": 0.1},
        "models": {
            "glm-5.3-flash": {
                "input_usd_per_million": 0.2,
                "cached_input_usd_per_million": 0.05,
                "output_usd_per_million": 1.0,
                "routable_via_gateway": True,
                "monthly_usd": 15.0,
            },
            "broken": {"input_usd_per_million": "not-a-number"},
        },
    }
    table_path = tmp_path / "opencode_go_models.json"
    table_path.write_text(json.dumps(table))
    monkeypatch.setattr(catalog, "_PRICE_TABLE", str(table_path))
    prices = catalog._price_map()
    assert prices["glm-5.3-flash"]["input_per_million_usd"] == 0.2
    assert "broken" not in prices
