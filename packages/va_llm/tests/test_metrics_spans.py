"""Gateway metrics and spans (specs/27 §3.3, §3.6; Decision P03-T13a rule 10)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from llm_client_helpers import (
    COMPLETIONS,
    PLATFORM_KEY,
    GatewayRig,
    completion_body,
    gateway_rig,
    hanging,
    scripted,
    user,
)
from obs_helpers import vendor_metric_rows
from opentelemetry.trace import SpanKind, StatusCode
from prometheus_client import Counter, Gauge, Histogram
from service_helpers import json_log_stream

from va_common.metrics import new_registry
from va_llm import CallType, LlmMetrics, LlmUnavailableError
from va_llm.metrics import (
    BREAKER_METRIC,
    DURATION_METRIC,
    REQUESTS_METRIC,
    SPECULATION_METRIC,
    STRUCTURED_METRIC,
)

OK = httpx.Response(200, json=completion_body(usage=(30, 7)))
KINDS = {Counter: "counter", Histogram: "histogram", Gauge: "gauge"}


def test_registered_metrics_equal_the_spec_rows() -> None:
    registry = new_registry(process_collectors=False)
    metrics = LlmMetrics(registry)
    registered = set()
    for collector in (
        metrics._requests,
        metrics._duration,
        metrics._breaker,
        metrics.speculation,
        metrics.structured_outputs,
    ):
        name = collector._name
        if isinstance(collector, Counter):
            name = f"{name}_total"
        registered.add((name, KINDS[type(collector)], tuple(collector._labelnames)))
    assert registered == vendor_metric_rows("va_llm_")
    assert {row[0] for row in registered} == {
        REQUESTS_METRIC,
        DURATION_METRIC,
        BREAKER_METRIC,
        SPECULATION_METRIC,
        STRUCTURED_METRIC,
    }


async def test_one_outcome_per_attempt_and_one_duration_per_call(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(COMPLETIONS).mock(
        side_effect=[
            httpx.Response(503),
            OK,
            httpx.Response(429, headers={"retry-after": "9"}),
            httpx.Response(400),
        ]
    )
    await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    with pytest.raises(LlmUnavailableError):
        await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    with pytest.raises(LlmUnavailableError):
        await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    outcomes = {
        outcome: rig.metric(REQUESTS_METRIC, call_type="router", model="rtr-model", outcome=outcome)
        for outcome in ("ok", "server_error", "rate_limited", "client_error")
    }
    assert outcomes == {"ok": 1, "server_error": 1, "rate_limited": 1, "client_error": 1}
    assert rig.metric(f"{DURATION_METRIC}_count", call_type="router") == 3


async def test_timeout_transport_parse_and_breaker_outcomes(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    stuck = hanging()
    llm_api.post(COMPLETIONS).mock(
        side_effect=scripted(
            stuck, httpx.ConnectError("x"), httpx.ConnectError("x"), httpx.Response(200, text="?")
        )
    )
    task = asyncio.create_task(rig.gateway.complete("router", org_id=rig.org_id, messages=[user()]))
    await rig.clock.wait_for_sleep(1.5)
    await rig.clock.run_for(1.5)
    for pending in (task, rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])):
        with pytest.raises(LlmUnavailableError):
            await pending
    with pytest.raises(LlmUnavailableError):
        await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    breaker = rig.gateway.breaker("platform", "rtr-model")
    breaker.record(success=False)
    breaker.record(success=False)
    with pytest.raises(LlmUnavailableError):
        await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    labels = {"call_type": "router", "model": "rtr-model"}
    assert rig.metric(REQUESTS_METRIC, **labels, outcome="timeout") == 1
    assert rig.metric(REQUESTS_METRIC, **labels, outcome="transport_error") == 2
    assert rig.metric(REQUESTS_METRIC, **labels, outcome="parse_error") == 1
    assert rig.metric(REQUESTS_METRIC, **labels, outcome="breaker_open") == 1
    assert rig.metric("va_llm_errors_total", model="rtr-model", kind="timeout") == 1
    assert rig.metric("va_llm_errors_total", model="rtr-model", kind="http") == 2
    assert rig.metric("va_llm_errors_total", model="rtr-model", kind="parse") == 1
    assert rig.metric(BREAKER_METRIC, provider="platform", model="rtr-model") == 2


async def test_breaker_gauge_follows_transitions(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=OK)
    await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    assert rig.metric(BREAKER_METRIC, provider="platform", model="rtr-model") == 0
    breaker = rig.gateway.breaker("platform", "rtr-model")
    for _ in range(5):
        breaker.record(success=False)
    assert rig.metric(BREAKER_METRIC, provider="platform", model="rtr-model") == 2
    await rig.clock.run_for(20)
    assert breaker.allow()
    assert rig.metric(BREAKER_METRIC, provider="platform", model="rtr-model") == 1


async def test_catalog_tokens_only_with_a_media_worker_catalog(llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=OK)
    async with gateway_rig(catalog=True) as rig:
        await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()])
        assert rig.metric("va_llm_tokens_total", model="dlg-model", direction="in") == 30
        assert rig.metric("va_llm_tokens_total", model="dlg-model", direction="out") == 7
    llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(503))
    async with gateway_rig(catalog=False) as rig:
        with pytest.raises(LlmUnavailableError):
            await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
        assert rig.registry.get_sample_value("va_llm_tokens_total", {"model": "x", "direction": "in"}) is None
        assert rig.metric(REQUESTS_METRIC, call_type="router", model="rtr-model", outcome="server_error") == 2


async def test_success_span(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(side_effect=[httpx.Response(503), OK])
    await rig.gateway.complete("dialogue", org_id=rig.org_id, messages=[user()])
    [span] = rig.spans()
    assert (span.name, span.kind, span.status.status_code) == (
        "llm.dialogue",
        SpanKind.CLIENT,
        StatusCode.UNSET,
    )
    assert dict(span.attributes or {}) == {
        "llm.call_type": "dialogue",
        "llm.provider": "platform",
        "llm.model": "fb-model",
        "llm.attempts": 2,
        "llm.fallback": True,
        "http.status_code": 200,
        "llm.tokens_in": 30,
        "llm.tokens_out": 7,
    }


async def test_failure_span(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(503))
    with pytest.raises(LlmUnavailableError):
        await rig.gateway.complete("extraction", org_id=rig.org_id, messages=[user()])
    [span] = rig.spans()
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "LlmUnavailableError"
    attributes = dict(span.attributes or {})
    assert (attributes["llm.attempts"], attributes["http.status_code"], attributes["llm.fallback"]) == (
        2,
        503,
        False,
    )
    assert "llm.tokens_in" not in attributes
    assert [event.name for event in span.events] == ["exception"]


async def test_breaker_skip_span_has_no_status_code(rig: GatewayRig) -> None:
    breaker = rig.gateway.breaker("platform", "rtr-model")
    for _ in range(5):
        breaker.record(success=False)
    with pytest.raises(LlmUnavailableError):
        await rig.gateway.complete("router", org_id=rig.org_id, messages=[user()])
    [span] = rig.spans()
    assert "http.status_code" not in dict(span.attributes or {})
    assert dict(span.attributes or {})["llm.attempts"] == 0


async def test_the_api_key_never_appears_in_errors_logs_or_reprs(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    stream = json_log_stream()
    llm_api.post(COMPLETIONS).mock(
        side_effect=[
            httpx.Response(401, json={"error": {"message": "bad key"}}),
            httpx.ConnectError("x"),
            httpx.Response(500),
        ]
    )
    errors = []
    call_types: tuple[CallType, ...] = ("router", "dialogue")
    for call_type in call_types:
        with pytest.raises(LlmUnavailableError) as caught:
            await rig.gateway.complete(call_type, org_id=rig.org_id, messages=[user()])
        errors.append(caught.value)
    text = " ".join(
        [stream.getvalue(), repr(rig.gateway.registry.platform), *(f"{e} {e.last!r}" for e in errors)]
    )
    assert PLATFORM_KEY not in text
    assert "**********" in repr(rig.gateway.registry.platform)
