from __future__ import annotations

import pytest

from omnigent.codex_model_vocabulary import codex_spawn_model


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-6-astra", "gpt-6-astra"),
        ("databricks-gpt-6-astra", "gpt-6-astra"),
        ("GPT-6-ASTRA", "gpt-6-astra"),
        # Preserve the established dotted-minor translation.
        ("databricks-gpt-5-6-luna", "gpt-5.6-luna"),
    ],
)
def test_codex_spawn_model_supports_major_only_astra_ids(model: str, expected: str) -> None:
    assert codex_spawn_model(model) == expected
