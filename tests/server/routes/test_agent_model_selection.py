"""Declared metadata must not become served-model or credential authority."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnigent.entities import Agent
from omnigent.server.routes.builtin_agents import _to_agent_object as catalog_object
from omnigent.server.routes.sessions.routes_permissions import _to_agent_object as session_object
from omnigent.spec.types import AgentSpec, ExecutorSpec


@pytest.mark.parametrize("serialize", [catalog_object, session_object])
@pytest.mark.parametrize(
    ("model", "resolution", "public"),
    [
        ("gpt-6.1-sol", "EXPLICIT_DECLARED", "gpt-6.1-sol"),
        ("claude-sonnet-5-5", "EXPLICIT_DECLARED", "claude-sonnet-5-5"),
        (None, "UNKNOWN_DEFAULT", None),
        ("default", "UNKNOWN_DEFAULT", None),
        ("openrouter/auto", "UNKNOWN_OPAQUE_ROUTE", "openrouter/auto"),
        ("sonnet", "UNKNOWN_OPAQUE_ROUTE", "sonnet"),
        ("openrouter/AUTO", "UNKNOWN_OPAQUE_ROUTE", "openrouter/AUTO"),
        ("github_pat_SYNTHETIC", "UNKNOWN_DYNAMIC_OR_PRIVATE_VALUE", None),
        ("ghp_SYNTHETIC", "UNKNOWN_DYNAMIC_OR_PRIVATE_VALUE", None),
        ("api_key_SYNTHETIC", "UNKNOWN_DYNAMIC_OR_PRIVATE_VALUE", None),
        ("${PRIVATE_MODEL}", "UNKNOWN_DYNAMIC_OR_PRIVATE_VALUE", None),
        ("https://user:password@example.com/model", "UNKNOWN_DYNAMIC_OR_PRIVATE_VALUE", None),
        ("sk-private-credential", "UNKNOWN_DYNAMIC_OR_PRIVATE_VALUE", None),
        ("model\nprivate", "UNKNOWN_DYNAMIC_OR_PRIVATE_VALUE", None),
    ],
)
def test_both_serializers_project_only_declared_intent(serialize, model, resolution, public):
    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-native"},
            model=model,
            connection={"api_key": "CONNECTION_SENTINEL"},
        ),
    )
    cache = Mock()
    cache.load.return_value = SimpleNamespace(spec=spec)
    agent = Agent(id="fixture", created_at=1, name="fixture", bundle_location="fixture-only")
    result = serialize(agent, cache)
    cache.load.assert_called_once_with("fixture", "fixture-only", expand_env=True)
    value = result.model_selection
    assert value.resolution == resolution
    assert value.declared_model == public
    assert value.served_model is None
    assert value.selection_authority == "DECLARED_ONLY"
    assert value.source_field == "parsed_agent_spec.executor.model"
    assert "CONNECTION_SENTINEL" not in result.model_dump_json()


@pytest.mark.parametrize("serialize", [catalog_object, session_object])
def test_failed_existing_load_has_no_extra_discovery(serialize):
    cache = Mock()
    cache.load.side_effect = ValueError("fixture load refusal")
    result = serialize(
        Agent(id="fixture", created_at=1, name="fixture", bundle_location="fixture-only"), cache
    )
    assert cache.load.call_count == 1
    assert result.model_selection.resolution == "UNKNOWN_UNREADABLE_SPEC"
    assert result.model_selection.declared_model is None


@pytest.mark.asyncio
async def test_both_http_resources_share_projection_without_extra_cache_reads(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx
    from fastapi import APIRouter, FastAPI

    from omnigent.server.routes import builtin_agents
    from omnigent.server.routes.sessions import routes_agent

    agent = Agent(id="fixture", created_at=1, name="fixture", bundle_location="fixture-only")
    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(
            type="omnigent", config={"harness": "claude-native"}, model="claude-sonnet-5-5"
        ),
    )
    cache = Mock()
    cache.load.return_value = SimpleNamespace(spec=spec)
    store = Mock()
    store.list.return_value = SimpleNamespace(
        data=[agent], first_id="fixture", last_id="fixture", has_more=False
    )
    store.get.return_value = agent
    monkeypatch.setattr(builtin_agents, "_require_user", lambda *args: "fixture-user")
    monkeypatch.setattr(routes_agent, "_require_user", lambda *args: "fixture-user")
    monkeypatch.setattr(
        routes_agent,
        "_require_access_and_level",
        AsyncMock(return_value=SimpleNamespace(conversation=SimpleNamespace(agent_id="fixture"))),
    )
    app = FastAPI()
    app.include_router(builtin_agents.create_builtin_agents_router(store, cache), prefix="/v1")
    router = APIRouter()
    routes_agent.register_agent_routes(
        router, conversation_store=Mock(), agent_store=store, agent_cache=cache
    )
    app.include_router(router, prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        catalog = await client.get("/v1/agents")
        session = await client.get("/v1/sessions/fixture-session/agent")
    assert catalog.status_code == session.status_code == 200
    left = catalog.json()["data"][0]["model_selection"]
    assert left == session.json()["model_selection"]
    assert left["served_model"] is None
    assert left["declared_model"] == "claude-sonnet-5-5"
    assert cache.load.call_count == 2  # Existing one load per response; no added discovery.


@pytest.mark.parametrize("harness", ["kimi-native", "docloop", "grok", "acp", "devin"])
def test_opaque_harness_model_is_not_selection_authority(harness):
    from omnigent.server.routes._agent_model_selection import project_agent_model_selection

    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(
            type="omnigent", config={"harness": harness}, model="current-looking-model"
        ),
    )
    result = project_agent_model_selection(spec)
    assert result.resolution == "UNKNOWN_OPAQUE_ROUTE"
    assert result.served_model is None


def test_legacy_llm_normalization_has_only_parsed_field_provenance(tmp_path):
    from omnigent.server.routes._agent_model_selection import project_agent_model_selection
    from omnigent.spec.parser import parse

    (tmp_path / "config.yaml").write_text(
        "spec_version: 1\nexecutor:\n  type: omnigent\n"
        "  config:\n    harness: claude-native\nllm:\n  model: claude-sonnet-5-5\n"
    )
    result = project_agent_model_selection(parse(tmp_path, expand_env=False))
    assert result.declared_model == "claude-sonnet-5-5"
    assert result.source_field == "parsed_agent_spec.executor.model"
    assert result.served_model is None
