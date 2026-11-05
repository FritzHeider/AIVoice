"""The query embedding cache (specs/10 §10; specs/03 §11 ``emb:q:{sha256}``; Decision P03-T14b rule 6)."""

from __future__ import annotations

from typing import Any

import fakeredis
import httpx
import orjson
import respx
from llm_client_helpers import EMBED_MODEL, EMBEDDINGS, EmbeddingsRig, embedding_body, embeddings_rig
from redis_ttl import assert_expires_in
from service_helpers import json_log_stream, log_events

from va_llm.embedding_cache import QUERY_CACHE_TTL_S
from va_llm.embedding_wire import query_cache_key


async def _cached(rig: EmbeddingsRig, key: str) -> Any:
    raw = await rig.redis.get(key)
    assert raw is not None
    return orjson.loads(raw)


def _route(llm_api: respx.MockRouter) -> respx.Route:
    return llm_api.post(EMBEDDINGS).mock(return_value=httpx.Response(200, json=embedding_body(1, value=5.0)))


async def test_a_miss_embeds_and_caches_for_24_hours(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    route = _route(llm_api)
    vector = await erig.embeddings.embed_query("where is my order?", org_id=erig.org_id)
    assert vector == [1.0, 0.0, 0.0, 0.0]
    key = query_cache_key(EMBED_MODEL, 4, "where is my order?")
    assert await _cached(erig, key) == vector
    assert QUERY_CACHE_TTL_S == 86_400
    await assert_expires_in(erig.redis, key, QUERY_CACHE_TTL_S)
    assert route.call_count == 1
    assert erig.metric("va_llm_embedding_cache_total", result="miss") == 1
    (record,) = erig.usage.records
    assert (record.purpose, record.document_id, record.prompt_tokens) == ("query", None, 7)
    assert sent_input(route) == ["where is my order?"]


def sent_input(route: respx.Route) -> list[str]:
    inputs: list[str] = orjson.loads(route.calls[-1].request.content)["input"]
    return inputs


async def test_a_hit_sends_nothing(erig: EmbeddingsRig, llm_api: respx.MockRouter) -> None:
    route = _route(llm_api)
    first = await erig.embeddings.embed_query("hours", org_id=erig.org_id)
    second = await erig.embeddings.embed_query("hours", org_id=erig.org_id)
    assert first == second
    assert route.call_count == 1
    assert erig.metric("va_llm_embedding_cache_total", result="hit") == 1
    assert erig.metric("va_llm_embedding_duration_ms_count", purpose="query") == 1
    assert len(erig.usage.records) == 1


async def test_other_texts_models_and_dimensions_miss(llm_api: respx.MockRouter) -> None:
    server = fakeredis.FakeServer()
    route = llm_api.post(EMBEDDINGS).mock(
        side_effect=lambda request: httpx.Response(
            200, json=embedding_body(1, orjson.loads(request.content)["dimensions"])
        )
    )
    async with embeddings_rig(redis=fakeredis.FakeAsyncRedis(server=server)) as rig:
        await rig.embeddings.embed_query("hours", org_id=rig.org_id)
        await rig.embeddings.embed_query("Hours", org_id=rig.org_id)
        await rig.embeddings.embed_query("hours", org_id=rig.org_id)
    async with embeddings_rig(redis=fakeredis.FakeAsyncRedis(server=server), model="other") as rig:
        await rig.embeddings.embed_query("hours", org_id=rig.org_id)
    async with embeddings_rig(redis=fakeredis.FakeAsyncRedis(server=server), dimensions=8) as rig:
        await rig.embeddings.embed_query("hours", org_id=rig.org_id)
    assert route.call_count == 4


async def test_a_malformed_cached_value_is_a_miss_and_is_overwritten(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    route = _route(llm_api)
    key = query_cache_key(EMBED_MODEL, 4, "hours")
    for corrupt in (b"not json", b"[1, 2]", b'["a", "b", "c", "d"]'):
        await erig.redis.set(key, corrupt)
        assert await erig.embeddings.embed_query("hours", org_id=erig.org_id) == [1.0, 0.0, 0.0, 0.0]
        assert await _cached(erig, key) == [1.0, 0.0, 0.0, 0.0]
    assert route.call_count == 3
    assert erig.metric("va_llm_embedding_cache_total", result="miss") == 3


async def test_a_long_query_is_truncated_but_keyed_by_its_text(
    erig: EmbeddingsRig, llm_api: respx.MockRouter
) -> None:
    route = _route(llm_api)
    text = "q" * 40_000
    await erig.embeddings.embed_query(text, org_id=erig.org_id)
    assert sent_input(route) == ["q" * 32_000]
    assert await erig.redis.exists(query_cache_key(EMBED_MODEL, 4, text)) == 1


async def test_redis_down_fails_open(llm_api: respx.MockRouter) -> None:
    stream = json_log_stream()
    route = _route(llm_api)
    server = fakeredis.FakeServer()
    server.connected = False
    async with embeddings_rig(redis=fakeredis.FakeAsyncRedis(server=server)) as rig:
        assert await rig.embeddings.embed_query("hours", org_id=rig.org_id) == [1.0, 0.0, 0.0, 0.0]
        assert await rig.embeddings.embed_query("hours", org_id=rig.org_id) == [1.0, 0.0, 0.0, 0.0]
        assert rig.metric("va_llm_embedding_cache_total", result="error") == 2
    assert route.call_count == 2
    events = [event for event in log_events(stream) if event["event"] == "llm.embedding_cache_unavailable"]
    assert [event["operation"] for event in events] == ["get", "set", "get", "set"]
    assert all(event["error"] == "ConnectionError" for event in events)
