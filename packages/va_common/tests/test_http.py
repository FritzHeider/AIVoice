from __future__ import annotations

import httpx
import pytest
import respx
from obs_helpers import make_settings, memory_provider

from va_common.http import DEFAULT_CONNECT_TIMEOUT_S, build_timeout, http_client, user_agent


async def test_float_timeout_caps_connect_phase() -> None:
    async with http_client(make_settings(), timeout=30.0) as client:
        assert client.timeout == httpx.Timeout(30.0, connect=DEFAULT_CONNECT_TIMEOUT_S)
    async with http_client(make_settings(), timeout=2.0) as client:
        assert client.timeout == httpx.Timeout(2.0, connect=2.0)


async def test_explicit_timeout_object_is_kept() -> None:
    custom = httpx.Timeout(1.0, read=9.0)
    async with http_client(make_settings(), timeout=custom) as client:
        assert client.timeout == custom


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_non_positive_timeout_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        build_timeout(value)


async def test_headers_base_url_redirects_and_limits() -> None:
    settings = make_settings()
    async with http_client(
        settings,
        timeout=5.0,
        base_url="https://hooks.example.test/v1",
        headers={"X-Extra": "1"},
        version="1.2.3",
        http2=True,
    ) as client:
        assert client.headers["User-Agent"] == "voiceagents-telephony_edge/1.2.3"
        assert client.headers["X-Extra"] == "1"
        assert client.base_url == httpx.URL("https://hooks.example.test/v1/")
        assert client.follow_redirects is False


def test_user_agent_override() -> None:
    assert user_agent(make_settings(), "9") == "voiceagents-telephony_edge/9"


@respx.mock
async def test_redirect_is_not_followed() -> None:
    respx.get("https://a.example.test/start").mock(
        return_value=httpx.Response(302, headers={"Location": "https://a.example.test/next"})
    )
    async with http_client(make_settings(), timeout=5.0, headers={"User-Agent": "custom"}) as client:
        response = await client.get("https://a.example.test/start")
    assert response.status_code == 302
    assert response.request.headers["User-Agent"] == "custom"


async def test_limits_are_applied_to_default_transport() -> None:
    async with http_client(make_settings(), timeout=5.0, max_connections=3, max_keepalive=1) as client:
        pool = client._transport._pool  # type: ignore[attr-defined]  # inspecting httpx internals on purpose
        assert pool._max_connections == 3
        assert pool._max_keepalive_connections == 1


async def test_requests_are_traced_and_propagate_traceparent() -> None:
    provider, exporter = memory_provider()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    tracer = provider.get_tracer("test")
    async with http_client(
        make_settings(), timeout=5.0, transport=httpx.MockTransport(handler), tracer_provider=provider
    ) as client:
        with tracer.start_as_current_span("parent") as parent:
            response = await client.post("https://api.example.test/things")
    assert response.status_code == 200
    spans = {span.name: span for span in exporter.get_finished_spans()}
    client_span = spans["POST"]
    attributes = client_span.attributes or {}
    assert attributes.get("http.request.method", attributes.get("http.method")) == "POST"
    assert client_span.parent is not None
    assert client_span.parent.span_id == parent.get_span_context().span_id
    trace_id = f"{parent.get_span_context().trace_id:032x}"
    assert trace_id in seen[0].headers["traceparent"]
