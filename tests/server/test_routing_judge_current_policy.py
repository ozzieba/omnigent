from unittest.mock import patch

import httpx
import pytest

from omnigent.server.smart_routing import ExternalRoutingClient


async def invoke(client, callback=None):
    calls = []
    original = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"route_selection": []})

    client._resolve_credentials = callback or (lambda: (None, {}))
    with (
        patch.object(
            httpx,
            "AsyncClient",
            lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
        ),
    ):
        value = await client.route("public synthetic fixture", {"codex": ["gpt-6-luna"]})
    return value, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "gpt-5-4-mini",
        "gpt-5.5",
        "databricks-gpt-5-4",
        "system.ai.gpt-5.6-terra",
        "Gpt-5.5-Pro",
        "custom.gpt-5.4-pro",
    ],
)
async def test_old_override_refuses_before_auth_or_http(model):
    client = ExternalRoutingClient(
        base_url="https://fixture.invalid",
        router_name="task_v0",
        selection_model=model,
        model_prefixes=["custom."],
    )

    def forbidden():
        raise AssertionError("credentials must not resolve")

    result, calls = await invoke(client, forbidden)
    assert result is None and calls == []
    assert "superseded" in client.last_error


@pytest.mark.asyncio
async def test_auth_callback_drift_refuses_before_http():
    client = ExternalRoutingClient(
        base_url="https://fixture.invalid", router_name="task_v0", selection_model="gpt-6-luna"
    )

    def drift():
        client._selection_model = "databricks-gpt-5.4-mini"
        return None, {}

    _, calls = await invoke(client, drift)
    assert calls == [] and "superseded" in client.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra", None])
async def test_current_and_upstream_unqualified_default_keep_protocol(model):
    import json

    client = ExternalRoutingClient(
        base_url="https://fixture.invalid", router_name="task_v0", selection_model=model
    )
    _, calls = await invoke(client)
    assert len(calls) == 1
    selector = json.loads(calls[0].content)["route_selector"]
    assert selector.get("config") == ({"model": model} if model is not None else None)
