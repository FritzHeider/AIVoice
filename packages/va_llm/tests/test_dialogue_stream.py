"""Streaming dialogue (specs/10 §2, §2.1, §8; Decision P03-T13b), with respx on a manual clock."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import respx
from llm_client_helpers import (
    COMPLETIONS,
    ORG_COMPLETIONS,
    BodyPart,
    GatewayRig,
    Hanging,
    StaticOrgProviders,
    chunk,
    collect,
    content_stream,
    dialogue,
    gateway_rig,
    hanging,
    make_provider,
    open_breaker,
    org_provider,
    scripted,
    sent_json,
    sse,
    streamed,
)
from opentelemetry.trace import StatusCode

from va_llm import (
    DialogueStream,
    LlmConnectionError,
    LlmHttpError,
    LlmProtocolError,
    LlmTimeoutError,
    LlmUnavailableError,
    OrgProviderError,
    ToolCall,
    ToolDef,
)
from va_llm.dialogue import TOTAL_S, TTFT_S


def _models(route: respx.Route) -> list[str]:
    return [sent_json(call.request)["model"] for call in route.calls]


def _ok(*tokens: str) -> httpx.Response:
    return streamed(*sse(*content_stream(*tokens)))[0]


async def _start(rig: GatewayRig, **fields: object) -> DialogueStream:
    return await rig.gateway.stream_dialogue(dialogue(rig.org_id, **fields))


async def _expire_ttft(rig: GatewayRig) -> None:
    await rig.clock.wait_for_sleep(TTFT_S)
    await rig.clock.run_for(TTFT_S)


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


# Happy path.


async def test_streams_deltas_with_usage_finish_reason_and_request_body(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=_ok("Hi", " there", "."))
    call_id = uuid.uuid4()
    async with await _start(rig, call_id=call_id, turn_index=3) as stream:
        assert await collect(stream) == ["Hi", " there", "."]
        assert await stream.tool_calls() == []
    assert (stream.finish_reason, stream.error, stream.fell_back, stream.attempts) == ("stop", None, False, 1)
    assert (stream.provider_id, stream.model) == ("platform", "dlg-model")
    assert stream.usage is not None
    assert (stream.usage.prompt_tokens, stream.usage.completion_tokens, stream.usage.estimated) == (
        12,
        5,
        False,
    )
    assert stream.ttft_ms == 0.0
    [call] = route.calls
    body = sent_json(call.request)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert (body["model"], body["temperature"], body["max_tokens"]) == ("dlg-model", 0.7, 250)
    assert "tools" not in body
    assert "tool_choice" not in body
    assert call.request.headers["x-va-call-id"] == str(call_id)


async def test_tools_are_sent_with_tool_choice_auto_and_calls_assembled(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    chunks = [
        chunk({"role": "assistant"}),
        chunk(
            {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "end_call", "arguments": ""}}]}
        ),
        chunk({"tool_calls": [{"index": 1, "id": "call_b", "function": {"name": "transfer"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"reason":'}}]}),
        chunk({"tool_calls": [{"index": 1, "function": {"arguments": "{}"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"done"}'}}]}),
        chunk(finish_reason="tool_calls"),
    ]
    route = llm_api.post(COMPLETIONS).mock(return_value=streamed(*sse(*chunks))[0])
    tool = ToolDef(name="end_call", description="End the call")
    async with await _start(rig, tools=(tool,)) as stream:
        assert await collect(stream) == []
        calls = await stream.tool_calls()
    assert calls == [
        ToolCall(id="call_a", name="end_call", arguments_json='{"reason":"done"}'),
        ToolCall(id="call_b", name="transfer", arguments_json="{}"),
    ]
    assert stream.finish_reason == "tool_calls"
    assert stream.ttft_ms == 0.0
    body = sent_json(route.calls[0].request)
    assert body["tool_choice"] == "auto"
    assert body["tools"] == [tool.wire()]
    assert "x-va-call-id" not in route.calls[0].request.headers
    usage = stream.usage
    assert usage is not None
    assert usage.estimated is True


async def test_ttft_is_measured_on_the_clock_and_skips_reasoning_deltas(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    thinking, token = asyncio.Event(), asyncio.Event()
    reasoning = sse(
        chunk({"role": "assistant", "content": None, "reasoning_content": "Let me think"}), done=False
    )
    rest = sse(*content_stream("Sure."))
    llm_api.post(COMPLETIONS).mock(return_value=streamed(thinking, *reasoning, token, *rest)[0])
    async with await _start(rig) as stream:
        reader = asyncio.create_task(collect(stream))
        await rig.clock.wait_for_sleep(TTFT_S)
        await rig.clock.run_for(0.4)
        thinking.set()
        await _settle()
        assert stream.ttft_ms is None
        await rig.clock.run_for(0.6)
        token.set()
        assert await reader == ["Sure."]
    assert stream.ttft_ms == pytest.approx(1000.0)


async def test_a_provider_and_model_in_the_request_are_used(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(ORG_COMPLETIONS).mock(return_value=_ok("Yo"))
    provider = make_provider("byo", "http://org-llm.test/v1", "byo-key")
    async with await _start(rig, provider=provider, model="custom") as stream:
        assert await collect(stream) == ["Yo"]
    assert _models(route) == ["custom"]
    assert (stream.provider_id, stream.model) == ("byo", "custom")


async def test_comments_blank_lines_and_other_fields_are_ignored(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    parts = [b": keep-alive\n\n", b"event: message\nid: 7\n", *sse(*content_stream("A"))]
    llm_api.post(COMPLETIONS).mock(return_value=streamed(*parts)[0])
    async with await _start(rig) as stream:
        assert await collect(stream) == ["A"]
    assert stream.error is None


# Fallback before the first token.


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(503),
        httpx.Response(429, headers={"retry-after": "0"}),
        httpx.Response(400),
        httpx.ConnectError("refused"),
    ],
    ids=["503", "429", "400", "connect"],
)
async def test_failures_before_the_first_token_fall_back_without_retry(
    rig: GatewayRig, llm_api: respx.MockRouter, first: httpx.Response | Exception
) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=scripted(first, _ok("Backup")))
    async with await _start(rig) as stream:
        assert await collect(stream) == ["Backup"]
    assert _models(route) == ["dlg-model", "fb-model"]
    assert (stream.fell_back, stream.attempts, stream.model, stream.error) == (True, 2, "fb-model", None)


@pytest.mark.parametrize(
    "parts",
    [
        sse(*content_stream("never"), done=False)[:1],
        [b"data: {not json\n\n"],
        [b'data: {"error": {"message": "overloaded"}}\n\n'],
        [b'data: {"choices": {"delta": 1}}\n\n'],
    ],
    ids=["no-done", "not-json", "error-chunk", "bad-choices"],
)
async def test_a_broken_stream_before_the_first_token_falls_back(
    rig: GatewayRig, llm_api: respx.MockRouter, parts: list[bytes]
) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=scripted(streamed(*parts)[0], _ok("Backup")))
    async with await _start(rig) as stream:
        assert await collect(stream) == ["Backup"]
    assert _models(route) == ["dlg-model", "fb-model"]


async def _late_answer(
    rig: GatewayRig, route: respx.Route, first: httpx.Response | Hanging
) -> DialogueStream:
    """The first request never produces a token; after the TTFT deadline the fallback answers ``Late``."""
    route.mock(side_effect=scripted(first, _ok("Late")))
    async with await _start(rig) as stream:
        reader = asyncio.create_task(collect(stream))
        await _expire_ttft(rig)
        assert await reader == ["Late"]
    return stream


async def test_ttft_deadline_falls_back_and_closes_the_first_stream(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    stuck, body = streamed(asyncio.Event())
    route = llm_api.post(COMPLETIONS)
    stream = await _late_answer(rig, route, stuck)
    assert (body.cancelled, body.closed) == (True, True)
    assert _models(route) == ["dlg-model", "fb-model"]
    assert stream.ttft_ms == 0.0  # measured from the fallback's own send


async def test_a_hanging_request_hits_the_ttft_deadline(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    stuck = hanging()
    await _late_answer(rig, llm_api.post(COMPLETIONS), stuck)
    assert stuck.cancelled == 1


async def test_the_fallback_has_its_own_ttft_deadline(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    first, second = streamed(asyncio.Event()), streamed(asyncio.Event())
    llm_api.post(COMPLETIONS).mock(side_effect=scripted(first[0], second[0]))
    async with await _start(rig) as stream:
        reader = asyncio.create_task(collect(stream))
        await _expire_ttft(rig)
        await _expire_ttft(rig)
        with pytest.raises(LlmUnavailableError) as caught:
            await reader
        with pytest.raises(LlmUnavailableError):
            await stream.tool_calls()
    assert caught.value.reason == "exhausted"
    assert isinstance(caught.value.last, LlmTimeoutError)
    assert stream.error is caught.value
    assert second[1].closed


async def test_both_targets_failing_raises_unavailable(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(503))
    async with await _start(rig) as stream:
        with pytest.raises(LlmUnavailableError) as caught:
            await collect(stream)
    assert route.call_count == 2
    assert (caught.value.reason, caught.value.model) == ("exhausted", "fb-model")
    assert isinstance(caught.value.last, LlmHttpError)
    assert rig.usage.records == []
    assert stream.usage is None


async def test_no_second_attempt_when_the_fallback_is_the_primary(llm_api: respx.MockRouter) -> None:
    models = {"dialogue": "same", "router": "r", "analysis": "a", "fallback": "same"}
    route = llm_api.post(COMPLETIONS).mock(return_value=httpx.Response(500))
    async with gateway_rig(platform=make_provider(models=models)) as rig:
        async with await _start(rig) as stream:
            with pytest.raises(LlmUnavailableError):
                await collect(stream)
        assert route.call_count == 1
        assert stream.attempts == 1


async def test_an_open_primary_breaker_goes_straight_to_the_fallback(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    open_breaker(rig, "dlg-model")
    route = llm_api.post(COMPLETIONS).mock(return_value=_ok("Backup"))
    async with await _start(rig) as stream:
        assert await collect(stream) == ["Backup"]
    assert _models(route) == ["fb-model"]
    assert (stream.attempts, stream.fell_back) == (1, True)


async def test_both_breakers_open_raises_breaker_open(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    open_breaker(rig, "dlg-model")
    open_breaker(rig, "fb-model")
    route = llm_api.post(COMPLETIONS)
    async with await _start(rig) as stream:
        with pytest.raises(LlmUnavailableError) as caught:
            await collect(stream)
    assert (caught.value.reason, caught.value.last, route.call_count) == ("breaker_open", None, 0)


async def test_an_org_provider_falls_back_to_the_platform_fallback(llm_api: respx.MockRouter) -> None:
    org_id = uuid.uuid4()
    org_route = llm_api.post(ORG_COMPLETIONS).mock(return_value=httpx.Response(502))
    route = llm_api.post(COMPLETIONS).mock(return_value=_ok("Platform"))
    providers = StaticOrgProviders({org_id: org_provider(org_id)})
    async with (
        gateway_rig(org_providers=providers) as rig,
        await rig.gateway.stream_dialogue(dialogue(org_id)) as stream,
    ):
        assert await collect(stream) == ["Platform"]
    assert (_models(org_route), _models(route)) == (["org-dlg-model"], ["fb-model"])


class _BrokenOrgProviders:
    async def get(self, org_id: uuid.UUID) -> None:
        raise OrgProviderError(provider_id=f"org:{org_id}", cause="DecryptionError")


async def test_an_unloadable_org_provider_streams_from_the_platform_fallback(
    llm_api: respx.MockRouter,
) -> None:
    route = llm_api.post(COMPLETIONS).mock(side_effect=scripted(httpx.Response(503)))
    async with gateway_rig(org_providers=_BrokenOrgProviders()) as rig, await _start(rig) as stream:
        with pytest.raises(LlmUnavailableError) as caught:
            await collect(stream)
    assert _models(route) == ["fb-model"]
    assert (stream.fell_back, stream.attempts, caught.value.reason) == (True, 1, "exhausted")


async def test_an_unloadable_org_provider_answer(llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=_ok("Fallback"))
    async with gateway_rig(org_providers=_BrokenOrgProviders()) as rig, await _start(rig) as stream:
        assert await collect(stream) == ["Fallback"]
    assert (stream.fell_back, stream.model) == (True, "fb-model")


# After the first token.


async def test_a_break_after_the_first_token_keeps_the_partial_text(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    chunks = sse(*content_stream("One", " two"), done=False)[:3]
    tool = chunk(
        {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "end_call", "arguments": "{"}}]}
    )
    nameless = chunk({"tool_calls": [{"index": 1, "id": "d"}]})
    response, _ = streamed(*chunks, *sse(tool, nameless, done=False), httpx.ReadError("reset"))
    route = llm_api.post(COMPLETIONS).mock(side_effect=scripted(response, _ok("unused")))
    async with await _start(rig) as stream:
        assert await collect(stream) == ["One", " two"]
        assert await stream.tool_calls() == [ToolCall(id="c", name="end_call", arguments_json="{")]
    assert isinstance(stream.error, LlmConnectionError)
    assert (stream.finish_reason, route.call_count, stream.fell_back) == (None, 1, False)
    [record] = rig.usage.records
    assert record.estimated is True
    assert record.model == "dlg-model"


async def test_a_read_timeout_after_the_first_token_truncates(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    parts: list[BodyPart] = [*sse(*content_stream("Hey"), done=False)[:2], httpx.ReadTimeout("slow")]
    llm_api.post(COMPLETIONS).mock(return_value=streamed(*parts)[0])
    async with await _start(rig) as stream:
        assert await collect(stream) == ["Hey"]
    assert isinstance(stream.error, LlmTimeoutError)


class _CrashingOrgProviders:
    async def get(self, org_id: uuid.UUID) -> None:
        msg = f"bug for {org_id}"
        raise RuntimeError(msg)


async def test_an_unexpected_producer_crash_reaches_the_consumer() -> None:
    async with gateway_rig(org_providers=_CrashingOrgProviders()) as rig:
        stream = await _start(rig)
        with pytest.raises(RuntimeError, match="bug for"):
            await collect(stream)
        with pytest.raises(RuntimeError, match="bug for"):
            await stream.tool_calls()
        with pytest.raises(RuntimeError, match="bug for"):
            await stream.aclose()
        [span] = rig.spans()
        assert dict(span.attributes or {})["llm.outcome"] == "failed"
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "RuntimeError"
        assert [event.name for event in span.events] == ["exception"]


async def test_a_protocol_error_after_the_first_token_truncates(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    parts = [*sse(*content_stream("Hey"), done=False)[:2], b"data: nope\n\n"]
    llm_api.post(COMPLETIONS).mock(return_value=streamed(*parts)[0])
    async with await _start(rig) as stream:
        assert await collect(stream) == ["Hey"]
    assert isinstance(stream.error, LlmProtocolError)


async def test_the_total_deadline_truncates_a_stalled_stream(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    stall = asyncio.Event()
    response, body = streamed(*sse(*content_stream("Part"), done=False)[:2], stall)
    llm_api.post(COMPLETIONS).mock(return_value=response)
    async with await _start(rig) as stream:
        reader = asyncio.create_task(collect(stream))
        await rig.clock.wait_for_sleep(TOTAL_S)
        await rig.clock.run_for(TOTAL_S)
        assert await reader == ["Part"]
    assert isinstance(stream.error, LlmTimeoutError)
    assert body.closed
    assert len(rig.usage.records) == 1


async def test_a_completed_stream_without_tokens_is_metered(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=_ok())
    async with await _start(rig) as stream:
        assert await collect(stream) == []
    assert (stream.ttft_ms, stream.error, stream.finish_reason) == (None, None, "stop")
    [record] = rig.usage.records
    assert (record.prompt_tokens, record.completion_tokens, record.estimated) == (12, 5, False)


async def test_a_nameless_tool_call_in_a_finished_stream_falls_back(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    nameless = sse(chunk({"tool_calls": [{"index": 0, "id": "x", "function": {"arguments": "{}"}}]}))
    route = llm_api.post(COMPLETIONS).mock(side_effect=scripted(streamed(*nameless)[0], _ok("Fine")))
    async with await _start(rig) as stream:
        assert await collect(stream) == []
        assert await stream.tool_calls() == []
    assert isinstance(stream.error, LlmProtocolError)
    assert route.call_count == 1


# Cancellation.


async def test_cancelling_the_consumer_closes_the_stream_without_a_clock_wait(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    response, body = streamed(*sse(*content_stream("Hello"), done=False)[:2], asyncio.Event())
    llm_api.post(COMPLETIONS).mock(return_value=response)
    stream = await _start(rig)
    deltas: list[str] = []

    async def consume() -> None:
        async for delta in stream.text():
            deltas.append(delta)

    reader = asyncio.create_task(consume())
    await rig.clock.wait_for_sleep(TOTAL_S)
    await _settle()  # the body now waits inside the hang
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert (deltas, body.closed, body.cancelled) == (["Hello"], True, True)
    assert stream.error is None
    [record] = rig.usage.records
    assert record.estimated is True
    await stream.aclose()


async def test_aclose_before_the_first_token_frees_the_half_open_probe(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    open_breaker(rig, "dlg-model")
    breaker = rig.gateway.breaker("platform", "dlg-model")
    await rig.clock.run_for(20.0)
    response, body = streamed(asyncio.Event())
    llm_api.post(COMPLETIONS).mock(return_value=response)
    stream = await _start(rig)
    await rig.clock.wait_for_sleep(TTFT_S)
    assert breaker.state == "half_open"
    await stream.aclose()
    assert body.closed
    assert breaker.allow()  # the probe slot is free again
    assert rig.usage.records == []


async def test_breaking_out_of_text_closes_the_stream(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    response, body = streamed(*sse(*content_stream("A", "B"), done=False)[:2], asyncio.Event())
    llm_api.post(COMPLETIONS).mock(return_value=response)
    stream = await _start(rig)
    text = stream.text()
    assert await anext(text) == "A"
    await text.aclose()  # type: ignore[attr-defined]
    assert body.closed
    await stream.aclose()


async def test_text_may_be_iterated_once(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(COMPLETIONS).mock(return_value=_ok("x"))
    async with await _start(rig) as stream:
        assert await collect(stream) == ["x"]
        with pytest.raises(RuntimeError, match="only once"):
            stream.text()


# Speculative generation.


async def test_speculative_deltas_wait_for_commit(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    more = asyncio.Event()
    parts: list[BodyPart] = [
        *sse(*content_stream("A", "B"), done=False)[:3],
        more,
        *sse(*content_stream("C"))[1:],
    ]
    llm_api.post(COMPLETIONS).mock(return_value=streamed(*parts)[0])
    async with await _start(rig, speculative=True) as stream:
        reader = asyncio.create_task(collect(stream))
        await rig.clock.wait_for_sleep(TOTAL_S)
        await _settle()
        assert not reader.done()
        assert stream.ttft_ms == 0.0
        stream.commit()
        stream.commit()
        more.set()
        assert await reader == ["A", "B", "C"]
        await stream.discard()
    assert rig.metric("va_llm_speculation_total", outcome="committed") == 1
    assert rig.metric("va_llm_speculation_total", outcome="discarded") == 0


async def _finished_speculation(rig: GatewayRig, llm_api: respx.MockRouter) -> DialogueStream:
    llm_api.post(COMPLETIONS).mock(return_value=_ok("A", "B"))
    stream = await _start(rig, speculative=True)
    await stream.tool_calls()
    return stream


async def test_commit_after_the_end_releases_everything(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    async with await _finished_speculation(rig, llm_api) as stream:
        stream.commit()
        assert await collect(stream) == ["A", "B"]


async def test_discard_closes_the_request_and_drops_the_tokens(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    response, body = streamed(*sse(*content_stream("A", "B"), done=False)[:3], asyncio.Event())
    llm_api.post(COMPLETIONS).mock(return_value=response)
    async with await _start(rig, speculative=True) as stream:
        reader = asyncio.create_task(collect(stream))
        await rig.clock.wait_for_sleep(TOTAL_S)
        await stream.discard()
        assert body.closed
        assert await reader == []
        await stream.discard()
        stream.commit()
    assert rig.metric("va_llm_speculation_total", outcome="discarded") == 1
    assert rig.metric("va_llm_speculation_total", outcome="committed") == 0
    [record] = rig.usage.records
    assert (record.estimated, record.completion_tokens) == (True, 1)
    assert stream.error is None


async def test_discard_after_the_end_ends_a_waiting_consumer(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    async with await _finished_speculation(rig, llm_api) as stream:
        reader = asyncio.create_task(collect(stream))
        await _settle()
        assert not reader.done()
        await stream.discard()
        assert await reader == []
    assert rig.metric("va_llm_speculation_total", outcome="discarded") == 1


async def test_text_after_discarding_a_finished_stream_ends_empty(
    rig: GatewayRig, llm_api: respx.MockRouter
) -> None:
    async with await _finished_speculation(rig, llm_api) as stream:
        await stream.discard()
        assert await collect(stream) == []
        assert stream.error is None


async def test_commit_and_discard_on_a_plain_stream(rig: GatewayRig, llm_api: respx.MockRouter) -> None:
    response, body = streamed(*sse(*content_stream("A"), done=False)[:2], asyncio.Event())
    llm_api.post(COMPLETIONS).mock(return_value=response)
    async with await _start(rig) as stream:
        stream.commit()
        await rig.clock.wait_for_sleep(TOTAL_S)
        await stream.discard()
        assert body.closed
        assert await collect(stream) == ["A"]
    assert rig.metric("va_llm_speculation_total", outcome="discarded") == 0
    assert rig.metric("va_llm_speculation_total", outcome="committed") == 0
