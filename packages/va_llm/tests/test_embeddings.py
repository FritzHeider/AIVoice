"""The embeddings client against respx (specs/10 §10, §11; Decision P03-T14b rules 1-5, 7, 8)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
import respx
from llm_client_helpers import (
    EMBED_MODEL,
    EMBEDDINGS,
    PLATFORM_KEY,
    EmbeddingsRig,
    Hanging,
    embedding_body,
    embeddings_rig,
    hanging,
    scripted,
    sent_json,
)
from service_helpers import json_log_stream, log_events

from va_llm import Embeddings, LlmHttpError, LlmProtocolError, LlmUnavailableError
from va_llm.embedding_wire import MAX_INPUT_BYTES
from va_llm.embeddings import ATTEMPT_TIMEOUT_S, EMBED_BATCH_SIZE


def _texts(count: int, prefix: str = "text") -> list[str]:
    return [f"{prefix} {index}" for index in range(count)]


def _answer_by_size(request: httpx.Request) -> httpx.Response:
    """One vector per input; vector ``i`` of a batch points along axis ``i % 4``."""
    inputs = sent_json(request)["input"]
    data = [
        {"index": index, "embedding": [3.0 if axis == index % 4 else 0.0 for axis in range(4)]}
        for index in range(len(inputs))
    ]
    return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": len(inputs)}})


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _embed(rig: EmbeddingsRig, texts: list[str], **fields: Any) -> list[list[float]]:
    return await rig.embeddings.embed(texts, org_id=rig.org_id, **fields)


async def _after_waits[T](rig: EmbeddingsRig, task: asyncio.Task[T], *waits: float) -> T:
    """Run ``rig``'s clock through each wait (as soon as something sleeps it), then ``task``'s result."""
    for wait in waits:
        await rig.clock.wait_for_sleep(wait)
        await rig.clock.run_for(wait)
    return await task


async def _unavailable(rig: EmbeddingsRig, texts: list[str]) -> LlmUnavailableError:
    with pytest.raises(LlmUnavailableError) as caught:
        await _embed(rig, texts)
    return caught.value


# Batching and request shape.


async def test_inputs_are_batched_by_64_in_order(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(EMBEDDINGS).mock(side_effect=_answer_by_size)
    texts = _texts(130)
    vectors = await _embed(erig, texts)
    sent = [sent_json(call.request) for call in route.calls]
    assert sorted(len(body["input"]) for body in sent) == [2, 64, 64]
    assert sorted(item for body in sent for item in body["input"]) == sorted(texts)
    for body in sent:
        assert body["model"] == EMBED_MODEL
        assert body["dimensions"] == 4
        assert body["encoding_format"] == "float"
    assert len(vectors) == 130
    expected = [
        [1.0 if axis == (index % EMBED_BATCH_SIZE) % 4 else 0.0 for axis in range(4)] for index in range(130)
    ]
    assert vectors == expected
    assert route.calls[0].request.headers["authorization"] == f"Bearer {PLATFORM_KEY}"


async def test_an_empty_list_sends_nothing(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(EMBEDDINGS).mock(side_effect=_answer_by_size)
    assert await _embed(erig, []) == []
    assert route.call_count == 0
    assert erig.metric("va_llm_embedding_duration_ms_count", purpose="ingest") == 0


async def test_an_empty_text_is_refused(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(EMBEDDINGS).mock(side_effect=_answer_by_size)
    with pytest.raises(ValueError, match="non-empty"):
        await _embed(erig, ["fine", ""])
    with pytest.raises(ValueError, match="non-empty"):
        await erig.embeddings.embed_query("", org_id=erig.org_id)
    assert route.call_count == 0


async def test_at_most_four_batches_are_in_flight(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    stuck = hanging(httpx.Response(200, json=embedding_body(EMBED_BATCH_SIZE)))
    llm_api.post(EMBEDDINGS).mock(side_effect=stuck)
    task = asyncio.create_task(_embed(erig, _texts(10 * EMBED_BATCH_SIZE)))
    async with asyncio.timeout(5.0):
        await stuck.wait_started(4)
    await _settle()
    assert stuck.started == 4
    stuck.release.set()
    vectors = await task
    assert (len(vectors), stuck.started) == (640, 10)


async def test_long_inputs_are_truncated_before_sending(llm_api: respx.MockRouter) -> None:
    stream = json_log_stream()
    route = llm_api.post(EMBEDDINGS).mock(side_effect=_answer_by_size)
    long_text = "word " * 10_000
    async with embeddings_rig() as rig:
        await _embed(rig, ["short", long_text])
    sent = sent_json(route.calls[0].request)["input"]
    assert sent[0] == "short"
    assert len(sent[1].encode()) == MAX_INPUT_BYTES
    assert long_text.startswith(sent[1])
    (event,) = [event for event in log_events(stream) if event["event"] == "llm.embedding_truncated"]
    assert (event["inputs"], event["truncated"]) == (2, 1)
    assert "word" not in str(event)


# Retries.


async def test_server_errors_are_retried_after_1_then_4_seconds(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(EMBEDDINGS).mock(
        side_effect=scripted(
            httpx.Response(503), httpx.Response(500), httpx.Response(200, json=embedding_body(1))
        )
    )
    task = asyncio.create_task(_embed(erig, ["a"]))
    assert await _after_waits(erig, task, 1.0, 4.0) == [[1.0, 0.0, 0.0, 0.0]]
    assert route.call_count == 3
    assert erig.metric("va_llm_embedding_requests_total", model=EMBED_MODEL, outcome="server_error") == 2
    assert erig.metric("va_llm_embedding_requests_total", model=EMBED_MODEL, outcome="ok") == 1


async def test_four_failures_exhaust_after_1_4_and_10_seconds(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(502))
    error = await _after_waits(erig, asyncio.create_task(_unavailable(erig, ["a"])), 1.0, 4.0, 10.0)
    assert route.call_count == 4
    assert error.reason == "exhausted"
    assert isinstance(error.last, LlmHttpError)
    assert error.last.status == 502
    assert (error.provider_id, error.model) == ("platform", EMBED_MODEL)
    assert erig.metric("va_llm_errors_total", model=EMBED_MODEL, kind="http") == 4


async def test_a_429_waits_for_its_retry_after(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    route = llm_api.post(EMBEDDINGS).mock(
        side_effect=scripted(
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(429, headers={"retry-after": "90"}),
            httpx.Response(200, json=embedding_body(1)),
        )
    )
    await _after_waits(erig, asyncio.create_task(_embed(erig, ["a"])), 7.0, 30.0)  # 90 s capped at 30 s
    assert route.call_count == 3
    assert erig.metric("va_llm_embedding_requests_total", model=EMBED_MODEL, outcome="rate_limited") == 2


async def test_a_short_retry_after_keeps_the_scheduled_wait(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(EMBEDDINGS).mock(
        side_effect=scripted(httpx.Response(429), httpx.Response(200, json=embedding_body(1)))
    )
    await _after_waits(erig, asyncio.create_task(_embed(erig, ["a"])), 1.0)


@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_client_errors_are_not_retried(
    erig: EmbeddingsRig, llm_api: respx.MockRouter, status: int
) -> None:
    route = llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(status))
    error = await _unavailable(erig, ["a"])
    assert route.call_count == 1
    assert isinstance(error.last, LlmHttpError)
    assert error.last.status == status
    assert erig.metric("va_llm_embedding_requests_total", model=EMBED_MODEL, outcome="client_error") == 1


async def test_a_wrong_dimension_is_a_protocol_error_without_retry(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    route = llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(200, json=embedding_body(1, 3)))
    error = await _unavailable(erig, ["a"])
    assert route.call_count == 1
    assert isinstance(error.last, LlmProtocolError)
    assert error.error_kind == "parse"
    assert erig.metric("va_llm_embedding_requests_total", model=EMBED_MODEL, outcome="parse_error") == 1
    assert erig.metric("va_llm_errors_total", model=EMBED_MODEL, kind="parse") == 1


async def test_a_hung_attempt_times_out_after_30_seconds_and_is_retried(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    stuck = hanging()
    route = llm_api.post(EMBEDDINGS).mock(
        side_effect=scripted(stuck, httpx.Response(200, json=embedding_body(1)))
    )
    await _after_waits(erig, asyncio.create_task(_embed(erig, ["a"])), ATTEMPT_TIMEOUT_S, 1.0)
    assert (stuck.started, stuck.cancelled, route.call_count) == (1, 1, 1)
    assert erig.metric("va_llm_embedding_requests_total", model=EMBED_MODEL, outcome="timeout") == 1
    assert erig.metric("va_llm_errors_total", model=EMBED_MODEL, kind="timeout") == 1


async def test_connect_errors_are_retried(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(EMBEDDINGS).mock(
        side_effect=scripted(httpx.ConnectError("refused"), httpx.Response(200, json=embedding_body(1)))
    )
    await _after_waits(erig, asyncio.create_task(_embed(erig, ["a"])), 1.0)
    assert erig.metric("va_llm_embedding_requests_total", model=EMBED_MODEL, outcome="transport_error") == 1


async def test_a_failed_batch_cancels_the_batches_in_flight(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    stuck = Hanging()
    llm_api.post(EMBEDDINGS).mock(side_effect=scripted(stuck, stuck, httpx.Response(400)))
    with pytest.raises(LlmUnavailableError) as caught:
        await _embed(erig, _texts(3 * EMBED_BATCH_SIZE))
    assert isinstance(caught.value.last, LlmHttpError)
    assert (stuck.started, stuck.cancelled) == (2, 2)
    assert erig.usage.records == []


# Metering.


async def test_each_batch_is_metered(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(EMBEDDINGS).mock(side_effect=_answer_by_size)
    document_id = uuid.uuid4()
    await _embed(erig, _texts(70), document_id=document_id)
    records = sorted(erig.usage.records, key=lambda record: record.prompt_tokens)
    assert [record.prompt_tokens for record in records] == [6, 64]
    for record in records:
        assert (record.org_id, record.document_id, record.purpose) == (erig.org_id, document_id, "ingest")
        assert (record.provider_id, record.model, record.estimated) == ("platform", EMBED_MODEL, False)
    assert [record.cost_micros for record in records] == [1, 2]  # 30 micros / 1k tokens, rounded up
    assert erig.metric("va_llm_tokens_total", model=EMBED_MODEL, direction="in") == 70


async def test_missing_usage_is_estimated(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(200, json=embedding_body(2, usage=None)))
    await _embed(erig, ["abcd" * 3, "x"])
    (record,) = erig.usage.records
    assert (record.prompt_tokens, record.estimated) == (4, True)


async def test_a_recorder_failure_is_swallowed(llm_api: respx.MockRouter) -> None:
    stream = json_log_stream()
    llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(200, json=embedding_body(1)))
    async with embeddings_rig() as rig:
        rig.usage.failure = RuntimeError("database down")
        assert await _embed(rig, ["a"]) == [[1.0, 0.0, 0.0, 0.0]]
    (event,) = [event for event in log_events(stream) if event["event"] == "llm.usage_record_failed"]
    assert event["error"] == "RuntimeError"


async def test_without_a_recorder_only_metrics_count(llm_api: respx.MockRouter) -> None:
    llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(200, json=embedding_body(1)))
    async with embeddings_rig() as rig:
        bare = Embeddings(
            rig.embeddings.provider,
            httpx.AsyncClient(),
            redis=rig.redis,
            clock=rig.clock,
            metrics=rig.embeddings._metrics,
            model=EMBED_MODEL,
            dimensions=4,
        )
        assert await bare.embed(["a"], org_id=rig.org_id) == [[1.0, 0.0, 0.0, 0.0]]
        assert rig.metric("va_llm_tokens_total", model=EMBED_MODEL, direction="in") == 7


# Metrics, spans, secrecy.


async def test_one_duration_per_call(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(EMBEDDINGS).mock(side_effect=_answer_by_size)
    await _embed(erig, _texts(130))
    assert erig.metric("va_llm_embedding_duration_ms_count", purpose="ingest") == 1
    assert erig.metric("va_llm_embedding_requests_total", model=EMBED_MODEL, outcome="ok") == 3


async def test_a_failed_call_still_observes_its_duration(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(400))
    await _unavailable(erig, ["a"])
    assert erig.metric("va_llm_embedding_duration_ms_count", purpose="ingest") == 1


async def test_one_span_per_batch(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(EMBEDDINGS).mock(side_effect=_answer_by_size)
    await _embed(erig, _texts(65))
    spans = erig.spans()
    assert [span.name for span in spans] == ["llm.embeddings", "llm.embeddings"]
    attributes = {(span.attributes or {})["llm.inputs"]: dict(span.attributes or {}) for span in spans}
    assert attributes[1] == {
        "llm.provider": "platform",
        "llm.model": EMBED_MODEL,
        "llm.purpose": "ingest",
        "llm.inputs": 1,
        "llm.attempts": 1,
        "http.status_code": 200,
        "llm.tokens_in": 1,
    }
    assert attributes[64]["llm.tokens_in"] == 64


async def test_a_failed_batch_span_has_error_status(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(422))
    await _unavailable(erig, ["a"])
    (span,) = erig.spans()
    assert span.status.status_code.name == "ERROR"
    assert span.attributes is not None
    assert (span.attributes["http.status_code"], span.attributes["llm.attempts"]) == (422, 1)
    assert "llm.tokens_in" not in span.attributes
    assert [event.name for event in span.events] == ["exception"]


async def test_a_timed_out_span_has_no_status_code(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    llm_api.post(EMBEDDINGS).mock(side_effect=scripted(*(hanging() for _ in range(4))))
    task = asyncio.create_task(_unavailable(erig, ["a"]))
    timeout = ATTEMPT_TIMEOUT_S
    error = await _after_waits(erig, task, timeout, 1.0, timeout, 4.0, timeout, 10.0, timeout)
    assert error.error_kind == "timeout"
    (span,) = erig.spans()
    assert span.attributes is not None
    assert "http.status_code" not in span.attributes
    assert span.attributes["llm.attempts"] == 4


async def test_the_api_key_never_appears_in_errors_or_logs(llm_api: respx.MockRouter) -> None:
    stream = json_log_stream()
    llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
    async with embeddings_rig() as rig:
        error = await _unavailable(rig, ["secret text"])
    rendered = f"{error} {error!r} {error.last} {stream.getvalue()} {rig.embeddings.provider!r}"
    assert PLATFORM_KEY not in rendered
    assert "secret text" not in stream.getvalue()
