"""Public projection of already-parsed model intent, with no discovery I/O."""

from __future__ import annotations

import re

from omnigent.server.schemas import AgentModelSelection
from omnigent.spec.types import AgentSpec

_PUBLIC_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")


def project_agent_model_selection(spec: AgentSpec) -> AgentModelSelection:
    """Project only the typed model; configuration, auth and routes stay private."""
    model = spec.executor.model
    if model is None or model == "" or model.lower() == "default":
        return AgentModelSelection(resolution="UNKNOWN_DEFAULT")
    if not _PUBLIC_MODEL.fullmatch(model) or any(
        marker in model.lower()
        for marker in (
            "sk-",
            "token",
            "secret",
            "password",
            "api-key",
            "api_key",
            "github_pat_",
            "ghp_",
            "glpat-",
            "xoxb-",
            "xoxp-",
            "akia",
        )
    ):
        return AgentModelSelection(resolution="UNKNOWN_DYNAMIC_OR_PRIVATE_VALUE")
    if model.lower() in {
        "auto",
        "sonnet",
        "opus",
        "haiku",
        "big-pickle",
    } or model.lower().endswith("/auto"):
        return AgentModelSelection(declared_model=model, resolution="UNKNOWN_OPAQUE_ROUTE")
    if spec.executor.harness_kind in {"kimi-native", "grok", "docloop", "acp", "devin"}:
        return AgentModelSelection(declared_model=model, resolution="UNKNOWN_OPAQUE_ROUTE")
    return AgentModelSelection(declared_model=model, resolution="EXPLICIT_DECLARED")
