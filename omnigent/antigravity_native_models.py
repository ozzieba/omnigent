"""Model-catalog discovery for native Antigravity (agy) sessions.

The catalog comes from the installed CLI itself: ``agy models`` asks the
signed-in account which models it is offered (``fetchAvailableModels``) and
prints one ``<id>\\t<label>`` row per model. It authenticates but starts no
agent turn, so listing spends no quota.

The ids are the exact spellings ``agy --model`` accepts. That matters because
agy does not reject an unrecognised ``--model``: it logs a warning and falls
back to its own default model, so a stale pin silently runs a different model.
A launch should pin an id taken from this listing.

Kept apart from :mod:`omnigent.antigravity_native_launch`, which is pure
argv assembly and never runs agy.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping

from omnigent.antigravity_native_launch import agy_binary_path

# A real agy model id: lower-case slug such as ``gemini-3.8-flash-high`` or
# ``claude-opus-5-5-high``. Anything else (spinner text, prose error lines
# that happen to contain a tab) is not a catalog row.
_AGY_MODEL_ID_RE = re.compile(r"[a-z0-9][a-z0-9.\-]*")


def parse_agy_models_output(output: str) -> list[dict[str, object]]:
    """Parse ``agy models`` stdout into launch-picker rows.

    :param output: The CLI's stdout, e.g.
        ``"gemini-3.8-flash-high\\tGemini 3.8 Flash (High)\\n"``.
    :returns: One ``{"id", "model", "displayName"}`` row per distinct model
        id, in CLI order. ``id`` and ``model`` are both the exact
        ``--model`` spelling.
    :raises ValueError: When the output holds no model rows.
    """
    rows: dict[str, dict[str, object]] = {}
    for raw_line in output.splitlines():
        model_id, sep, label = raw_line.partition("\t")
        model_id = model_id.strip()
        if not sep or not _AGY_MODEL_ID_RE.fullmatch(model_id):
            continue
        rows.setdefault(
            model_id,
            {"id": model_id, "model": model_id, "displayName": label.strip() or model_id},
        )
    if not rows:
        raise ValueError("agy models output did not contain any models")
    return list(rows.values())


def list_agy_model_options(
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 60.0,
) -> list[dict[str, object]]:
    """List the models the signed-in account is offered, via ``agy models``.

    :param env: Environment for the CLI; ``None`` inherits the caller's
        (``HOME`` selects which agy login answers).
    :param timeout_s: Wall-clock limit. The CLI starts a local language
        server and makes a network round trip, so allow a few seconds.
    :returns: The parsed rows, see :func:`parse_agy_models_output`.
    :raises RuntimeError: When agy is not installed.
    :raises subprocess.CalledProcessError: When the CLI exits non-zero.
    :raises subprocess.TimeoutExpired: When the CLI exceeds *timeout_s*.
    :raises ValueError: When the CLI printed no model rows.
    """
    completed = subprocess.run(
        [agy_binary_path(), "models"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        # agy subcommands have hung on an inherited, unclosed stdin pipe.
        stdin=subprocess.DEVNULL,
        env=dict(env) if env is not None else None,
    )
    return parse_agy_models_output(completed.stdout)
