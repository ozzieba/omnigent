"""LLMQ-backed model catalog for the OpenCode harness (host-side).

The launch picker's model rows come from the OpenCode CLI catalog
(``opencode models``, see ``list_opencode_cli_model_options``). That catalog
knows ids but not prices. This module adds what the fleet actually routes
through llmq:

- the live, routable model list from the llmq personal API gateway
  (``GET /api/opencode-go/w/<workstream>/v1/models`` — llmq proxies the
  upstream, so this is upstream-truth with llmq's routing applied);
- per-model prices in USD per million tokens from llmq's vendored
  opencode-go price table (the same table the quota controller settles
  bills against — see ``llm_quota_controller/opencode_go_pricing.py``).

Everything is cached: an in-process TTL cache plus a on-disk snapshot under
``~/.cache/omnigent/``. A gateway failure degrades to the last snapshot and
then to unpriced CLI rows, so the picker still renders.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_GATEWAY_URL = os.environ.get("LLMQ_GATEWAY_URL", "http://100.87.142.18:17645")
_GATEWAY_TOKEN_FILE = os.environ.get(
    "LLMQ_GATEWAY_TOKEN_FILE",
    str(Path.home() / ".config/oz-agent-fleet/llmq-api-gateway-token"),
)
_WORKSTREAM = os.environ.get("LLMQ_GATEWAY_WORKSTREAM", "agent-infra")
_UPSTREAM = os.environ.get("LLMQ_OPENCODE_GO_UPSTREAM", "opencode-go")
# The quota controller's vendored price table (read-only; oz-readable).
_PRICE_TABLE = os.environ.get(
    "LLMQ_OPENCODE_GO_TABLE",
    "/opt/llmq/current/src/llm_quota_controller/opencode_go_models.json",
)
_DISK_CACHE = Path(
    os.environ.get(
        "LLMQ_CATALOG_DISK_CACHE",
        str(Path.home() / ".cache/omnigent/llmq-opencode-models.json"),
    )
)
TTL_SECONDS = float(os.environ.get("LLMQ_CATALOG_TTL_SECONDS", "600"))
_TIMEOUT_S = 8.0

_cache: dict[str, Any] = {"fetched_at": 0.0, "models": []}


def _fetch_llmq_catalog(token: str) -> list[dict[str, Any]]:
    """The merged cross-upstream catalog: prices + pareto + eval scores."""
    url = f"{_GATEWAY_URL}/v1/catalog?sort=pareto"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("model"), str)]


_catalog_cache: dict[str, Any] = {"entries": {}, "fetched_at": 0.0}


def _catalog_by_key() -> dict[tuple[str, str], dict[str, Any]]:
    """(upstream, model) -> catalog entry; cached with the module TTL."""
    now = time.time()
    if _catalog_cache["entries"] and now - _catalog_cache["fetched_at"] < TTL_SECONDS:
        return _catalog_cache["entries"]
    token = _gateway_token()
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    if token:
        try:
            for entry in _fetch_llmq_catalog(token):
                entries[(str(entry.get("upstream")), str(entry.get("model")))] = entry
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _logger.warning("llmq catalog fetch failed: %s", exc)
    _catalog_cache["entries"] = entries
    _catalog_cache["fetched_at"] = now
    return entries


def _catalog_entry(upstream: str, bare: str) -> dict[str, Any] | None:
    by_key = _catalog_by_key()
    entry = by_key.get((upstream, bare))
    if entry is None:
        for (_up, model), candidate in by_key.items():
            if model == bare or model.endswith("/" + bare):
                return candidate
    return entry


def _decorate(display: str, entry: dict[str, Any] | None) -> str:
    """Embed price/eval/pareto into the picker label (no web rebuild needed)."""
    if not entry:
        return display
    in_usd = float(entry.get("input_usd_per_million") or 0)
    out_usd = float(entry.get("output_usd_per_million") or 0)
    price = "free" if in_usd == 0 and out_usd == 0 else f"${in_usd + out_usd:.2f}/M"
    bits = [price]
    if entry.get("eval_score") is not None:
        bits.append(f"{round(float(entry['eval_score']) * 100)}%")
    if entry.get("avg_latency_s") is not None:
        bits.append(f"{float(entry['avg_latency_s']):.1f}s")
    if entry.get("pareto"):
        bits.append("\u2605")
    return display + "  [" + " \u00b7 ".join(bits) + "]"


def _gateway_token() -> str | None:
    try:
        return Path(_GATEWAY_TOKEN_FILE).read_text().strip() or None
    except OSError:
        return None


def _price_map() -> dict[str, dict[str, float]]:
    """Bare opencode-go model id -> USD-per-million price triple."""
    try:
        raw = json.loads(Path(_PRICE_TABLE).read_bytes())
    except (OSError, ValueError) as exc:
        _logger.warning("llmq opencode-go price table unreadable at %s: %s", _PRICE_TABLE, exc)
        return {}
    out: dict[str, dict[str, float]] = {}
    for model_id, entry in raw.get("models", {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            out[model_id] = {
                "input_per_million_usd": float(entry["input_usd_per_million"]),
                "cached_input_per_million_usd": float(entry["cached_input_usd_per_million"]),
                "output_per_million_usd": float(entry["output_usd_per_million"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_llmq_models() -> list[dict[str, Any]]:
    token = _gateway_token()
    if token is None:
        _logger.warning("llmq gateway token missing at %s", _GATEWAY_TOKEN_FILE)
        return []
    url = f"{_GATEWAY_URL}/api/{_UPSTREAM}/w/{_WORKSTREAM}/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return [m for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)]


def _disk_cache_read() -> list[dict[str, Any]] | None:
    try:
        raw = json.loads(_DISK_CACHE.read_text())
        models = raw.get("models")
        if isinstance(models, list):
            return models
    except (OSError, ValueError):
        pass
    return None


def _disk_cache_write(models: list[dict[str, Any]]) -> None:
    try:
        _DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DISK_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "models": models}))
        tmp.replace(_DISK_CACHE)
    except OSError:
        pass


def llmq_opencode_models(force: bool = False) -> list[dict[str, Any]]:
    """Live opencode-go rows from llmq, priced, cached in-proc and on disk.

    Each row: ``{"id": "opencode-go/<bare>", "bare_id": ..., "pricing": {...}}``.
    Returns [] only when there is no live answer AND no cached snapshot.
    """
    now = time.time()
    if not force and _cache["models"] and now - _cache["fetched_at"] < TTL_SECONDS:
        return _cache["models"]
    try:
        live = _fetch_llmq_models()
        prices = _price_map()
        rows: list[dict[str, Any]] = []
        catalog_by_key = _catalog_by_key()
        for m in live:
            bare = str(m["id"])
            entry = catalog_by_key.get((_UPSTREAM, bare))
            row: dict[str, Any] = {
                "id": f"{_UPSTREAM}/{bare}",
                "bare_id": bare,
                "pricing": prices.get(bare),
                "eval_score": (entry or {}).get("eval_score"),
                "pareto": bool((entry or {}).get("pareto")),
                "avg_latency_s": (entry or {}).get("avg_latency_s"),
            }
            rows.append(row)
        _cache["fetched_at"] = now
        _cache["models"] = rows
        _disk_cache_write(rows)
        return rows
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _logger.warning("llmq opencode-go model fetch failed (%s); trying disk cache", exc)
    stale = _disk_cache_read()
    if stale is not None:
        _cache["fetched_at"] = now  # back off retries while serving stale
        _cache["models"] = stale
        return stale
    _cache["fetched_at"] = now
    _cache["models"] = []
    return []


def merge_llmq_pricing(cli_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich CLI catalog rows with llmq prices, then append llmq-only rows.

    - Rows whose id (or providerID/model pair) matches an llmq opencode-go
      model get a ``pricing`` dict (and keep their position).
    - llmq rows the CLI catalog does not know about are appended, marked
      ``source: "llmq"``.
    - Never raises: on any llmq failure the CLI rows pass through unpriced.
    """
    rows = [dict(r) for r in (cli_rows or []) if isinstance(r, dict)]
    try:
        llmq_rows = llmq_opencode_models()
    except Exception:  # noqa: BLE001 - the picker must render without llmq
        llmq_rows = []
    if not llmq_rows:
        return rows
    prices_by_full_id = {r["id"]: r.get("pricing") for r in llmq_rows}
    seen_full_ids: set[str] = set()
    for row in rows:
        full_id = str(row.get("id") or "")
        seen_full_ids.add(full_id)
        provider = str(row.get("providerID") or "")
        bare = str(row.get("model") or "")
        price = prices_by_full_id.get(full_id)
        if price is None and provider and bare:
            price = prices_by_full_id.get(f"{provider}/{bare}")
        if price is not None:
            row["pricing"] = price
    for llmq_row in llmq_rows:
        if llmq_row["id"] in seen_full_ids:
            continue
        display = llmq_row["id"]
        rows.append(
            {
                "id": display,
                "model": llmq_row["bare_id"],
                "providerID": _UPSTREAM,
                "displayName": _decorate(display, _catalog_entry(_UPSTREAM, llmq_row["bare_id"])),
                "name": llmq_row["bare_id"],
                "isDefault": False,
                "pricing": llmq_row.get("pricing"),
                "eval_score": llmq_row.get("eval_score"),
                "pareto": llmq_row.get("pareto"),
                "avg_latency_s": llmq_row.get("avg_latency_s"),
                "source": "llmq",
            }
        )

    # Cross-upstream pareto rows from the llmq catalog: curated to the
    # frontier so the picker stays navigable (the go lane rows above carry
    # their own catalog enrichment).
    try:
        catalog_entries = _catalog_by_key()
        go_bare = {r["bare_id"] for r in llmq_rows}
        appended_ids = {str(r.get("id") or "") for r in rows}
        for (upstream, model), entry in catalog_entries.items():
            if upstream == _UPSTREAM or model in go_bare:
                continue
            if not entry.get("pareto"):
                continue
            full = f"{upstream}/{model}"
            if full in appended_ids:
                continue
            in_usd = float(entry.get("input_usd_per_million") or 0)
            out_usd = float(entry.get("output_usd_per_million") or 0)
            price = {
                "input_per_million_usd": in_usd,
                "cached_input_per_million_usd": float(
                    entry.get("cached_input_per_million_usd") or 0
                ),
                "output_per_million_usd": out_usd,
            }
            decorated = _decorate(full, entry)
            rows.append(
                {
                    "id": full,
                    "model": model,
                    "providerID": upstream,
                    "displayName": decorated,
                    "name": model,
                    "isDefault": False,
                    "pricing": price,
                    "eval_score": entry.get("eval_score"),
                    "pareto": True,
                    "source": "llmq-catalog",
                }
            )
            appended_ids.add(full)
    except Exception:  # noqa: BLE001 - the picker must render without llmq
        pass
    return rows
