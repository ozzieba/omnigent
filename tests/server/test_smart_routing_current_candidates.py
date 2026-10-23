"""Current serving targets preserve external router wire labels and catalogs."""

from unittest.mock import patch

import pytest

from omnigent.model_fallbacks import SMART_ROUTING_SUPERSESSIONS
from omnigent.server.smart_routing import (
    RoutePick,
    RoutingResult,
    TaskV1RouteOptionSource,
    current_routing_candidate,
    route_session_harness,
    route_turn,
    substitute_model,
)
from tests.server.helpers import FakeCaps, FakeRoutingClient


@pytest.mark.parametrize(
    "old,current", [(k, v) for k, v in SMART_ROUTING_SUPERSESSIONS.items() if v]
)
def test_exact_old_catalog_hit_cannot_override_current(old, current):
    catalog = [f"databricks-{old}", f"databricks-{current}"]
    assert substitute_model(old, catalog, prefixes=("databricks-",), aliases={}) == catalog[1]


@pytest.mark.parametrize("old", list(SMART_ROUTING_SUPERSESSIONS))
def test_old_only_catalog_declines_without_inventing_endpoint(old):
    assert (
        substitute_model(old, [f"databricks-{old}"], prefixes=("databricks-",), aliases={}) is None
    )


def test_current_provider_override_retained_but_old_target_refused():
    model = "databricks-gpt-6-1-sol"
    assert (
        current_routing_candidate(
            model,
            [model],
            prefixes=("databricks-",),
            aliases={"gpt-6-1-sol": "system.ai.current-sol"},
        )
        == "system.ai.current-sol"
    )
    assert (
        current_routing_candidate(
            model,
            [model],
            prefixes=("databricks-",),
            aliases={"gpt-6-1-sol": "databricks-gpt-6-sol"},
        )
        is None
    )


def test_barred_current_successor_does_not_fall_back_to_old():
    assert (
        substitute_model(
            "gpt-6-sol",
            ["gpt-6-sol", "gpt-6.1-sol"],
            prefixes=(),
            aliases={},
            barred=("gpt-6.1-sol",),
        )
        is None
    )


@pytest.mark.parametrize(
    "model", ["gpt-5-nano", "openrouter/openai/gpt-5-nano", "databricks-gpt-5-nano"]
)
def test_nano_unique_cost_exception(model):
    assert (
        substitute_model(
            model, [model], prefixes=("openrouter/openai/", "databricks-"), aliases={}
        )
        == model
    )


def test_external_wire_menu_remains_exact_and_raw_pick_is_retained():
    source = TaskV1RouteOptionSource(model_prefixes=("databricks-",), servable_aliases={})
    catalog = {"codex": ["databricks-gpt-5-6-sol", "databricks-gpt-6-1-sol"]}
    options = source.build_route_options(["codex"], catalog)
    assert {"glm-5-2", "gpt-5-6-sol", "gpt-5-6-luna"}.issubset({x.model for x in options})
    result = source.resolve_selection(
        RoutePick(model="gpt-5-6-sol", harness="codex"), ["codex"], catalog
    )
    assert result is not None
    assert result.model == "databricks-gpt-6-1-sol"
    assert result.raw_model == "gpt-5-6-sol"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catalog,expected", [(["gpt-6-sol", "gpt-6.1-sol"], "gpt-6.1-sol"), (["gpt-6-sol"], None)]
)
async def test_builtin_judge_cannot_bypass_final_turn_fence(catalog, expected):
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model="gpt-6-sol", rationale="fixture", harness="codex")
        )
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        model, verdict = await route_turn(
            "codex",
            "synthetic",
            catalog=catalog,
            gateway_backed=False,
            allow_static_fallback=False,
        )
    assert model == expected
    if expected:
        assert verdict["raw_model"] == "gpt-6-sol"
    else:
        assert verdict is None


def test_already_resolved_current_provider_alias_is_idempotent():
    aliases = {"gpt-6-1-sol": "system.ai.current-sol"}
    assert (
        current_routing_candidate(
            "system.ai.current-sol",
            ["databricks-gpt-6-1-sol"],
            prefixes=("databricks-",),
            aliases=aliases,
        )
        == "system.ai.current-sol"
    )
    source = TaskV1RouteOptionSource(model_prefixes=("databricks-",), servable_aliases=aliases)
    result = source.resolve_selection(
        RoutePick(model="gpt-5-6-sol"), ["codex"], {"codex": ["databricks-gpt-6-1-sol"]}
    )
    assert result is not None and result.model == "system.ai.current-sol"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "catalog,expected", [(["gpt-6-sol", "gpt-6.1-sol"], "gpt-6.1-sol"), (["gpt-6-sol"], None)]
)
async def test_auto_harness_final_fence_preserves_family_and_catalog(catalog, expected):
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model="gpt-6-sol", rationale="fixture", harness="codex")
        )
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        harness, model, verdict, error = await route_session_harness(
            "synthetic",
            allowed_family="gpt",
            harness_candidates=("codex",),
            catalog={"codex": catalog},
            gateway_backed=False,
            allow_static_fallback=False,
        )
    assert model == expected
    if expected:
        assert harness == "codex" and error is None
        assert verdict["raw_model"] == "gpt-6-sol"
    else:
        assert harness is None and verdict is None and error


@pytest.mark.parametrize(
    "prefixes,model,old_alias",
    [
        (("databricks-",), "databricks-gpt-6-1-sol", "system.ai.gpt-6-sol"),
        (("system.ai.",), "system.ai.gpt-6-1-sol", "databricks-gpt-6-sol"),
    ],
)
def test_other_declared_provider_prefix_cannot_hide_superseded_alias(prefixes, model, old_alias):
    assert (
        current_routing_candidate(
            model, [model], prefixes=prefixes, aliases={"gpt-6-1-sol": old_alias}
        )
        is None
    )


@pytest.mark.parametrize(
    "old,current",
    [
        ("gpt-5-4-mini", "gpt-6-luna"),
        ("gpt-5-4", "gpt-6-1-sol"),
        ("gpt-5-5", "gpt-6-1-sol"),
        ("gpt-5-6-terra", "gpt-6-1-sol"),
        ("gpt-5-4-pro", "gpt-6-astra"),
        ("gpt-5-5-pro", "gpt-6-astra"),
    ],
)
def test_remaining_old_professional_routes_require_current_catalog(old, current):
    assert current_routing_candidate(old, [old], prefixes=(), aliases={}) is None
    assert current_routing_candidate(old, [old, current], prefixes=(), aliases={}) == current
    assert (
        current_routing_candidate(current, [current], prefixes=(), aliases={current: old}) is None
    )


@pytest.mark.parametrize("prefix", ["databricks-gpt-", "system.ai.gpt-"])
def test_overlapping_prefix_cannot_conceal_known_old_candidate_or_alias(prefix):
    for old in ("databricks-gpt-6-sol", "system.ai.gpt-6-sol"):
        assert current_routing_candidate(old, [old], prefixes=(prefix,), aliases={}) is None
        assert (
            current_routing_candidate(
                "gpt-6.1-sol",
                ["gpt-6.1-sol"],
                prefixes=(prefix,),
                aliases={"gpt-6-1-sol": old},
            )
            is None
        )


def test_missing_successor_uses_only_eligible_current_family_fallback():
    catalog = ["databricks-claude-opus-4-8", "databricks-claude-sonnet-5-5"]
    assert (
        substitute_model("claude-opus-4-8", catalog, prefixes=("databricks-",), aliases={})
        == catalog[1]
    )
    assert (
        substitute_model("claude-opus-4-8", catalog[:1], prefixes=("databricks-",), aliases={})
        is None
    )
    assert (
        substitute_model(
            "claude-opus-4-8", catalog, prefixes=("databricks-",), aliases={}, barred=(catalog[1],)
        )
        is None
    )
    assert (
        substitute_model(
            "claude-opus-4-8",
            catalog,
            prefixes=("databricks-",),
            aliases={"claude-sonnet-5-5": "system.ai.claude-sonnet-4-6"},
        )
        is None
    )


@pytest.mark.asyncio
async def test_missing_wire_compatible_harness_declines_before_current_lookup():
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model="gpt-6.1-sol", rationale="fixture", harness="codex")
        )
    )
    with (
        patch("omnigent.runtime._globals._caps", new=caps),
        patch("omnigent.server.smart_routing._redirect_wire_incompatible_pick", return_value=None),
    ):
        harness, model, verdict, error = await route_session_harness(
            "synthetic",
            allowed_family="gpt",
            harness_candidates=("codex",),
            catalog={"codex": ["gpt-6.1-sol"]},
            gateway_backed=False,
            allow_static_fallback=False,
        )
    assert (harness, model, verdict) == (None, None, None)
    assert error == "No available harness can run the selected wire protocol."
