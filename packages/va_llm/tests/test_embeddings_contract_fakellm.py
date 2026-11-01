"""Embeddings against FakeLLM (specs/10 §10 and §12, specs/30 §3.4; Decision 2026-11-01 P03-T14b)."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from llm_client_helpers import EmbeddingsRig, arm_fault, sim_embeddings
from llm_helpers import realtime_llm_state

from va_llm import LlmHttpError, LlmUnavailableError

type Sim = tuple[EmbeddingsRig, httpx.AsyncClient]


@pytest.fixture
async def sim() -> AsyncIterator[Sim]:
    async with sim_embeddings(realtime_llm_state()) as pair:
        yield pair


async def _served(control: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await control.get("/sim/llm/embeddings")
    requests: list[dict[str, Any]] = response.json()["requests"]
    return requests


def _norm(vector: list[float]) -> float:
    return math.sqrt(math.fsum(value * value for value in vector))


async def test_texts_become_deterministic_1024_dimension_unit_vectors(sim: Sim) -> None:
    rig, control = sim
    texts = [f"refund policy question number {index}" for index in range(70)]
    vectors = await rig.embeddings.embed(texts, org_id=rig.org_id)
    assert len(vectors) == 70
    assert all(len(vector) == 1024 for vector in vectors)
    assert all(math.isclose(_norm(vector), 1.0, rel_tol=1e-9) for vector in vectors)
    again = await rig.embeddings.embed([texts[69], texts[0]], org_id=rig.org_id)
    assert again == [vectors[69], vectors[0]]
    served = await _served(control)
    assert sorted(record["inputs"] for record in served) == [2, 6, 64]
    assert {(record["dimensions"], record["encoding_format"], record["status"]) for record in served} == {
        (1024, "float", 200)
    }


async def test_usage_is_the_simulators_prompt_tokens(sim: Sim) -> None:
    rig, _ = sim
    await rig.embeddings.embed(["one two three", "four five"], org_id=rig.org_id)
    (record,) = rig.usage.records
    assert (record.prompt_tokens, record.estimated) == (5, False)


async def test_a_query_embedding_matches_the_ingested_vector_and_is_cached(sim: Sim) -> None:
    rig, control = sim
    (ingested,) = await rig.embeddings.embed(["opening hours"], org_id=rig.org_id)
    query = await rig.embeddings.embed_query("opening hours", org_id=rig.org_id)
    assert query == ingested
    assert await rig.embeddings.embed_query("opening hours", org_id=rig.org_id) == query
    assert len(await _served(control)) == 2


async def test_a_rate_limit_is_retried_after_its_retry_after(sim: Sim) -> None:
    rig, control = sim
    await arm_fault(control, kind="rate_limit", endpoint="embeddings", retry_after_s=2)
    task = asyncio.create_task(rig.embeddings.embed(["hello"], org_id=rig.org_id))
    await rig.clock.wait_for_sleep(2.0)
    await rig.clock.run_for(2.0)
    (vector,) = await task
    assert len(vector) == 1024
    assert [record["status"] for record in await _served(control)] == [429, 200]


async def test_persistent_server_errors_exhaust_the_retries(sim: Sim) -> None:
    rig, control = sim
    await arm_fault(control, kind="server_error", endpoint="embeddings", times=4, status=503)
    task = asyncio.create_task(rig.embeddings.embed(["hello"], org_id=rig.org_id))
    for wait in (1.0, 4.0, 10.0):
        await rig.clock.wait_for_sleep(wait)
        await rig.clock.run_for(wait)
    with pytest.raises(LlmUnavailableError) as caught:
        await task
    assert isinstance(caught.value.last, LlmHttpError)
    assert caught.value.last.status == 503
    assert [record["status"] for record in await _served(control)] == [503] * 4
