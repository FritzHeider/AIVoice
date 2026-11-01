"""Providers, settings, the registry, cached clients, prewarm, and the probe (specs/10 §1; Decision P03-T13a
rules 2-4)."""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx
from llm_client_helpers import (
    BASE_URL,
    COMPLETIONS,
    PLATFORM_KEY,
    StaticOrgProviders,
    completion_body,
    make_provider,
    org_provider,
    sent_json,
)
from pydantic import ValidationError
from service_helpers import json_log_stream, log_events, settings_of

from va_llm import LlmClients, LlmSettings, ProbeResult, ProviderRegistry, platform_provider, probe_provider
from va_llm.probe import PROBE_TOOL

MODELS_URL = f"{BASE_URL}/models"
TOOL_CALL = [{"id": "c", "type": "function", "function": {"name": PROBE_TOOL, "arguments": '{"ok":true}'}}]


def test_platform_provider_from_the_spec_defaults() -> None:
    provider = platform_provider(settings_of(LlmSettings))
    assert (provider.id, provider.base_url, provider.api_key.get_secret_value()) == (
        "platform",
        "http://localhost:9300/v1",
        "sim-llm-key",
    )
    assert provider.models == {
        "dialogue": "zai-org/GLM-5.3-Flash",
        "router": "zai-org/GLM-5.3-Flash",
        "analysis": "zai-org/GLM-5.3",
        "fallback": "moonshotai/Kimi-K2.6",
    }
    assert (
        provider.max_concurrency,
        provider.supports_tool_choice,
        provider.supports_parallel_tool_calls,
    ) == (
        500,
        True,
        False,
    )


def test_platform_provider_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_DEFAULT_BASE_URL", "https://api.telnyx.com/v2/ai/openai/")
    monkeypatch.setenv("LLM_DEFAULT_API_KEY", "real-key")
    monkeypatch.setenv("LLM_ROUTER_MODEL", "router/x")
    monkeypatch.setenv(
        "LLM_MODEL_COSTS_JSON", '{"router/x": {"in_per_1k_micros": 1, "out_per_1k_micros": 1}}'
    )
    settings = settings_of(LlmSettings)
    provider = platform_provider(settings)
    assert provider.base_url == "https://api.telnyx.com/v2/ai/openai"
    assert provider.api_key.get_secret_value() == "real-key"
    assert provider.models["router"] == "router/x"
    assert "router/x" in settings.llm_model_costs_json
    assert "real-key" not in repr(settings)


@pytest.mark.parametrize("key", ["", "  "])
def test_a_provider_needs_an_api_key(key: str) -> None:
    with pytest.raises(ValidationError, match="api_key must not be empty"):
        make_provider(key=key)


@pytest.mark.parametrize(
    "models",
    [
        {},
        {"dialogue": "a", "router": "b", "analysis": "c"},
        {"dialogue": "a", "router": "b", "analysis": "c", "fallback": " "},
    ],
)
def test_a_provider_needs_a_model_for_every_role(models: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="every role"):
        make_provider(models=models)


async def test_registry_prefers_the_org_provider() -> None:
    org_id = uuid.uuid4()
    platform = make_provider()
    registry = ProviderRegistry(platform, StaticOrgProviders({org_id: org_provider(org_id)}))
    assert (await registry.resolve(org_id)).id == f"org:{org_id}"
    assert await registry.resolve(uuid.uuid4()) is platform
    assert await ProviderRegistry(platform).resolve(org_id) is platform


async def test_clients_are_cached_per_provider_and_rebuilt_on_key_rotation() -> None:
    async with httpx.AsyncClient() as http:
        clients = LlmClients(http)
        provider = make_provider()
        client = clients.client(provider)
        assert clients.client(make_provider()) is client
        rotated = clients.client(make_provider(key="rotated"))
        assert rotated is not client
        assert clients.client(make_provider(key="rotated")) is rotated
        assert clients.client(make_provider(base_url="http://other.test/v1", key="rotated")) is not rotated
        assert clients.client(make_provider("org:x")) is not clients.client(provider)
        assert (client.max_retries, str(client.base_url)) == (0, f"{BASE_URL}/")


async def test_concurrency_limits_follow_the_provider() -> None:
    async with httpx.AsyncClient() as http:
        clients = LlmClients(http)
        limit = clients.limit(make_provider(max_concurrency=2))
        assert clients.limit(make_provider(max_concurrency=2)) is limit
        assert clients.limit(make_provider(max_concurrency=3)) is not limit


async def test_prewarm_lists_models(llm_api: respx.MockRouter) -> None:
    route = llm_api.get(MODELS_URL).mock(
        return_value=httpx.Response(200, json={"object": "list", "data": []})
    )
    async with httpx.AsyncClient() as http:
        assert await LlmClients(http).prewarm(make_provider())
    assert route.calls.last.request.headers["authorization"] == f"Bearer {PLATFORM_KEY}"


async def test_prewarm_failures_are_logged_not_raised(llm_api: respx.MockRouter) -> None:
    stream = json_log_stream()
    llm_api.get(MODELS_URL).mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as http:
        assert not await LlmClients(http).prewarm(make_provider())
    [event] = [e for e in log_events(stream) if e["event"] == "llm.prewarm_failed"]
    assert (event["provider"], event["error"]) == ("platform", "APIConnectionError")


async def _probe(
    llm_api: respx.MockRouter, *responses: httpx.Response | Exception
) -> tuple[ProbeResult, respx.Route]:
    route = llm_api.post(COMPLETIONS).mock(side_effect=list(responses))
    async with httpx.AsyncClient() as http:
        return await probe_provider(make_provider(), http), route


async def test_probe_honoured_tool_choice(llm_api: respx.MockRouter) -> None:
    result, route = await _probe(
        llm_api, httpx.Response(200, json=completion_body(None, tool_calls=TOOL_CALL))
    )
    assert result == ProbeResult(supports_tool_choice=True, error=None)
    body = sent_json(route.calls.last.request)
    assert (body["model"], body["max_tokens"], body["temperature"]) == ("rtr-model", 16, 0)
    assert body["tool_choice"] == {"type": "function", "function": {"name": PROBE_TOOL}}
    assert body["tools"][0]["function"]["name"] == PROBE_TOOL


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(200, json=completion_body("ok")),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(400, json={"error": {"message": "tool_choice unsupported"}}),
        httpx.Response(422, json={"error": {"message": "tools unsupported"}}),
    ],
)
async def test_probe_without_forced_tools_retries_plainly(
    llm_api: respx.MockRouter, first: httpx.Response
) -> None:
    result, route = await _probe(llm_api, first, httpx.Response(200, json=completion_body("ok")))
    assert result == ProbeResult(supports_tool_choice=False, error=None)
    assert route.call_count == 2
    assert "tools" not in sent_json(route.calls.last.request)


async def test_probe_reports_failures_without_the_key(llm_api: respx.MockRouter) -> None:
    body = {"error": {"message": f"invalid key {PLATFORM_KEY}"}}
    result, route = await _probe(llm_api, httpx.Response(401, json=body))
    assert route.call_count == 1
    assert result.supports_tool_choice is False
    assert result.error is not None
    assert result.error.startswith("http: platform rtr-model:")
    assert PLATFORM_KEY not in result.error
    assert "***" in result.error


async def test_probe_reports_connection_and_retry_failures(llm_api: respx.MockRouter) -> None:
    result, _ = await _probe(llm_api, httpx.ConnectError("refused"))
    assert result.error is not None
    assert result.error.startswith("http:")
    result, _ = await _probe(llm_api, httpx.Response(400), httpx.Response(500))
    assert result.supports_tool_choice is False
    assert result.error is not None
    assert "500" in result.error
