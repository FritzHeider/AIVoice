"""Streaming dialogue metrics, spans, and metering (specs/27 §3.3, §3.6; Decision P03-T13b)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from llm_client_helpers import (
    COMPLETIONS,
    GatewayRig,
    collect,
    content_stream,
    dialogue,
    gateway_rig,
    sse,
    streamed,
)
from opentelemetry.trace import SpanKind, StatusCode

from va_llm import LlmUnavailableError
from va_llm.dialogue import TOTAL_S, TTFT_S
from va_llm.metrics import DURATION_METRIC, REQUESTS_METRIC

TTFT_METRIC = "va_llm_ttft_ms"


def _ok(*tokens: str, usage: tuple[int, int] | None = (30, 7)) -> httpx.Response:
    return streamed(*sse(*content_stream(*tokens, usage=usage)))[0]


async def test_attempt_outcomes_duration_ttft_and_tokens(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(side_effect=[httpx.Response(503), _ok("Hi")])
    async with await rig.gateway.stream_dialogue(dialogue(rig.org_id, turn_index=2)) as stream:
        assert await collect(stream) == ["Hi"]
    labels = {"call_type": "dialogue"}
    assert rig.metric(REQUESTS_METRIC, **labels, model="dlg-model", outcome="server_error") == 1
    assert rig.metric(REQUESTS_METRIC, **labels, model="fb-model", outcome="ok") == 1
    assert rig.metric(f"{DURATION_METRIC}_count", **labels) == 1
    assert rig.metric(f"{TTFT_METRIC}_count", model="fb-model", purpose="dialogue") == 1
    assert rig.metric(f"{TTFT_METRIC}_count", model="dlg-model", purpose="dialogue") == 0
    assert rig.metric("va_llm_errors_total", model="dlg-model", kind="http") == 1
    assert rig.metric("va_llm_tokens_total", model="fb-model", direction="in") == 30
    assert rig.metric("va_llm_tokens_total", model="fb-model", direction="out") == 7
    [record] = rig.usage.records
    assert (record.call_type, record.model, record.prompt_tokens, record.completion_tokens) == (
        "dialogue",
        "fb-model",
        30,
        7,
    )
    assert (record.cost_in_micros, record.cost_out_micros) == (30, 21)
    [span] = rig.spans()
    assert (span.name, span.kind) == ("llm.dialogue", SpanKind.CLIENT)
    assert span.status.status_code == StatusCode.UNSET
    expected = {
        "llm.call_type": "dialogue",
        "llm.streaming": True,
        "llm.speculative": False,
        "llm.outcome": "completed",
        "llm.turn_index": 2,
        "llm.ttft_ms": 0.0,
        "llm.provider": "platform",
        "llm.model": "fb-model",
        "llm.attempts": 2,
        "llm.fallback": True,
        "http.status_code": 200,
        "llm.tokens_in": 30,
        "llm.tokens_out": 7,
    }
    assert dict(span.attributes or {}) == expected


async def test_no_ttft_histogram_without_a_catalog(llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=_ok("Hi"))
    async with gateway_rig(catalog=False) as rig:
        async with await rig.gateway.stream_dialogue(dialogue(rig.org_id)) as stream:
            await collect(stream)
        assert (
            rig.registry.get_sample_value(
                f"{TTFT_METRIC}_count", {"model": "dlg-model", "purpose": "dialogue"}
            )
            is None
        )
        assert rig.metric(REQUESTS_METRIC, call_type="dialogue", model="dlg-model", outcome="ok") == 1


async def test_failed_stream_span_and_timeout_outcomes(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(
        side_effect=[streamed(asyncio.Event())[0], httpx.Response(200, text="data: x\n\n")]
    )
    async with await rig.gateway.stream_dialogue(dialogue(rig.org_id)) as stream:
        reader = asyncio.create_task(collect(stream))
        await rig.clock.wait_for_sleep(TTFT_S)
        await rig.clock.run_for(TTFT_S)
        with pytest.raises(LlmUnavailableError):
            await reader
    assert isinstance(stream.error, LlmUnavailableError)
    assert rig.metric(REQUESTS_METRIC, call_type="dialogue", model="dlg-model", outcome="timeout") == 1
    assert rig.metric(REQUESTS_METRIC, call_type="dialogue", model="fb-model", outcome="parse_error") == 1
    assert rig.metric("va_llm_errors_total", model="dlg-model", kind="timeout") == 1
    assert rig.metric("va_llm_errors_total", model="fb-model", kind="parse") == 1
    assert rig.usage.records == []
    [span] = rig.spans()
    attributes = dict(span.attributes or {})
    assert (attributes["llm.outcome"], attributes["llm.attempts"]) == ("failed", 2)
    assert "llm.ttft_ms" not in attributes
    assert "llm.tokens_in" not in attributes
    assert (span.status.status_code, span.status.description) == (StatusCode.ERROR, "LlmUnavailableError")
    assert [event.name for event in span.events] == ["exception"]


async def test_truncated_stream_span(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    response, _ = streamed(*sse(*content_stream("Part"), done=False)[:2], asyncio.Event())
    llm_api.post(COMPLETIONS).mock(return_value=response)
    async with await rig.gateway.stream_dialogue(dialogue(rig.org_id)) as stream:
        reader = asyncio.create_task(collect(stream))
        await rig.clock.wait_for_sleep(TOTAL_S)
        await rig.clock.run_for(TOTAL_S)
        await reader
    assert rig.metric(REQUESTS_METRIC, call_type="dialogue", model="dlg-model", outcome="timeout") == 1
    assert rig.metric(REQUESTS_METRIC, call_type="dialogue", model="dlg-model", outcome="ok") == 0
    [span] = rig.spans()
    attributes = dict(span.attributes or {})
    assert attributes["llm.outcome"] == "truncated"
    assert attributes["llm.tokens_out"] == 1
    assert (span.status.status_code, span.status.description) == (StatusCode.ERROR, "LlmTimeoutError")
    assert rig.metric(f"{DURATION_METRIC}_count", call_type="dialogue") == 1
    assert rig.metric(f"{DURATION_METRIC}_sum", call_type="dialogue") == TOTAL_S * 1000.0


async def test_cancelled_and_discarded_span_outcomes(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(side_effect=lambda _request: streamed(asyncio.Event())[0])
    stream = await rig.gateway.stream_dialogue(dialogue(rig.org_id))
    await stream.aclose()
    speculative = await rig.gateway.stream_dialogue(dialogue(rig.org_id, speculative=True))
    await rig.clock.wait_for_sleep(TTFT_S)
    await speculative.discard()
    outcomes = [dict(span.attributes or {})["llm.outcome"] for span in rig.spans()]
    assert outcomes == ["cancelled", "discarded"]
    assert all(span.status.status_code == StatusCode.UNSET for span in rig.spans())
    assert dict(rig.spans()[1].attributes or {})["llm.speculative"] is True
    assert rig.metric(f"{DURATION_METRIC}_count", call_type="dialogue") == 2


async def test_a_failing_recorder_never_fails_the_stream(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    rig.usage.failure = RuntimeError("billing down")
    llm_api.post(COMPLETIONS).mock(return_value=_ok("Hi", usage=None))
    async with await rig.gateway.stream_dialogue(dialogue(rig.org_id)) as stream:
        assert await collect(stream) == ["Hi"]
    assert stream.error is None
    usage = stream.usage
    assert usage is not None
    assert usage.estimated is True
