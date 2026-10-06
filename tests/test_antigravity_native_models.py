"""Tests for the Antigravity (agy) model-catalog discovery module.

No live agy calls are made: the CLI is replaced by a fake executable script
(so the real ``subprocess`` boundary is exercised) or by a monkeypatched
:func:`agy_binary_path`.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

import omnigent.antigravity_native_models as _mod
from omnigent.antigravity_native_models import (
    list_agy_model_options,
    parse_agy_models_output,
)

# Verbatim ``agy models`` stdout from agy 1.3.0 on a consumer login,
# captured 2026-10-06 (the spinner line is part of real output).
_AGY_1_3_0_MODELS_OUTPUT = (
    "Fetching available models...\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
    "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
    "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
    "claude-opus-5-5-high\tClaude Opus 5.5 (High)\n"
    "claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)\n"
    "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
)


class TestParseAgyModelsOutput:
    """Tests for :func:`parse_agy_models_output`."""

    def test_parses_every_tab_separated_row_in_cli_order(self) -> None:
        """Each ``id<TAB>label`` line becomes one picker row, order kept."""
        rows = parse_agy_models_output(_AGY_1_3_0_MODELS_OUTPUT)

        assert [row["id"] for row in rows] == [
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-low",
            "gemini-3.1-pro-high",
            "claude-opus-5-5-high",
            "claude-sonnet-5-5-medium",
            "gpt-oss-120b-medium",
        ]
        assert rows[4] == {
            "id": "claude-opus-5-5-high",
            "model": "claude-opus-5-5-high",
            "displayName": "Claude Opus 5.5 (High)",
        }

    def test_ignores_spinner_and_blank_lines(self) -> None:
        """Progress text and blank lines are not model rows."""
        rows = parse_agy_models_output("Fetching available models...\n\n  \nx-1\tX One\n")

        assert rows == [{"id": "x-1", "model": "x-1", "displayName": "X One"}]

    def test_duplicate_ids_keep_the_first_row(self) -> None:
        """A repeated id is listed once (the first label wins)."""
        rows = parse_agy_models_output("m-1\tFirst\nm-1\tSecond\n")

        assert rows == [{"id": "m-1", "model": "m-1", "displayName": "First"}]

    def test_missing_label_falls_back_to_the_id(self) -> None:
        """A row with an empty label still names the model by its id."""
        rows = parse_agy_models_output("m-1\t\n")

        assert rows == [{"id": "m-1", "model": "m-1", "displayName": "m-1"}]

    def test_rejects_output_with_no_model_rows(self) -> None:
        """No rows is an error, never an invented catalog."""
        with pytest.raises(ValueError, match="did not contain any models"):
            parse_agy_models_output("Fetching available models...\n")

    def test_rejects_ids_that_are_not_cli_model_ids(self) -> None:
        """Error/prose lines that happen to contain a tab are not rows."""
        with pytest.raises(ValueError, match="did not contain any models"):
            parse_agy_models_output("Error: not logged in\tplease run /login\n")


def _fake_agy(tmp_path: Path, script_body: str) -> Path:
    """Write an executable fake ``agy`` whose behaviour is *script_body*."""
    path = tmp_path / "agy"
    path.write_text("#!/bin/sh\n" + script_body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestListAgyModelOptions:
    """Tests for :func:`list_agy_model_options` across the subprocess boundary."""

    def test_runs_the_no_inference_models_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The CLI is invoked as exactly ``agy models`` and its rows returned."""
        argv_log = tmp_path / "argv"
        fake = _fake_agy(
            tmp_path,
            f'printf "%s\\n" "$@" > {argv_log}\nprintf "g-1\\tG One\\n"\n',
        )
        monkeypatch.setattr(_mod, "agy_binary_path", lambda: str(fake))

        rows = list_agy_model_options()

        assert rows == [{"id": "g-1", "model": "g-1", "displayName": "G One"}]
        assert argv_log.read_text().split() == ["models"]

    def test_stdin_is_closed_so_the_cli_cannot_hang_on_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """agy subcommands once hung on an inherited open stdin pipe."""
        fake = _fake_agy(tmp_path, 'cat >/dev/null\nprintf "g-1\\tG\\n"\n')
        monkeypatch.setattr(_mod, "agy_binary_path", lambda: str(fake))

        assert [row["id"] for row in list_agy_model_options(timeout_s=5)] == ["g-1"]

    def test_nonzero_exit_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A failing CLI surfaces as an error, not an empty catalog."""
        fake = _fake_agy(tmp_path, 'echo "boom" >&2\nexit 3\n')
        monkeypatch.setattr(_mod, "agy_binary_path", lambda: str(fake))

        with pytest.raises(subprocess.CalledProcessError):
            list_agy_model_options()

    def test_env_is_passed_through(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """An explicit env reaches the CLI (e.g. HOME selects the login)."""
        fake = _fake_agy(tmp_path, 'printf "%s\\tEnv\\n" "$AGY_TEST_MARK"\n')
        monkeypatch.setattr(_mod, "agy_binary_path", lambda: str(fake))

        rows = list_agy_model_options(env={**os.environ, "AGY_TEST_MARK": "env-1"})

        assert rows == [{"id": "env-1", "model": "env-1", "displayName": "Env"}]
