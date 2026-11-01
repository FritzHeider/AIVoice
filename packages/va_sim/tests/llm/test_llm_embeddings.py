"""FakeLLM ``POST /v1/embeddings`` (specs/30 §3.4; Decision P03-T06b rule 7)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx
import numpy as np
import pytest
from llm_helpers import API_KEY, MODEL, llm_client, llm_state, realtime_llm_state, served_llm, wait_held

from va_sim.llm.embeddings import DEFAULT_DIMENSIONS, embed, encode

AUTH = {"authorization": f"Bearer {API_KEY}"}


def cosine(left: str, right: str) -> float:
    return float(np.dot(embed(left), embed(right)))


def test_vectors_are_deterministic_unit_and_1024_wide() -> None:
    first = embed("book a table for two")
    assert first.dtype == np.float32
    assert first.shape == (DEFAULT_DIMENSIONS,)
    assert np.array_equal(first, embed("Book a table, for two!"))
    assert abs(float(np.linalg.norm(first)) - 1.0) < 1e-6
    assert embed("x", 256).shape == (256,)
    assert abs(float(np.linalg.norm(embed("", 8))) - 1.0) < 1e-6
    assert abs(float(np.linalg.norm(embed([1, 2, 3], 64))) - 1.0) < 1e-6


def test_similar_texts_score_higher() -> None:
    similar = cosine("book a table for two", "book a table for four")
    unrelated = cosine("book a table for two", "the invoice is overdue")
    assert similar > 0.7
    assert similar > unrelated


def test_encodings() -> None:
    vector = embed("hello world", 16)
    floats = encode(vector, "float")
    assert isinstance(floats, list)
    assert floats == [float(value) for value in vector]
    packed = encode(vector, "base64")
    assert isinstance(packed, str)
    assert np.array_equal(np.frombuffer(base64.b64decode(packed), dtype="<f4"), vector)


async def test_string_list_and_token_inputs() -> None:
    state = llm_state()
    async with llm_client(state) as client:
        single = await client.post("/v1/embeddings", json={"model": MODEL, "input": "hello there world"})
        listed = await client.post(
            "/v1/embeddings", json={"model": MODEL, "input": ["one two", "three"], "dimensions": 256}
        )
        tokens = await client.post("/v1/embeddings", json={"model": MODEL, "input": [5, 6, 7]})
        token_lists = await client.post("/v1/embeddings", json={"model": MODEL, "input": [[5, 6], [7]]})
        packed = await client.post(
            "/v1/embeddings",
            json={"model": MODEL, "input": "hello", "encoding_format": "base64", "user": "u"},
        )
    body = single.json()
    assert (body["object"], body["model"]) == ("list", MODEL)
    assert body["usage"] == {"prompt_tokens": 3, "total_tokens": 3}
    (item,) = body["data"]
    assert (item["object"], item["index"]) == ("embedding", 0)
    assert item["embedding"] == encode(embed("hello there world"), "float")
    assert [(entry["index"], len(entry["embedding"])) for entry in listed.json()["data"]] == [
        (0, 256),
        (1, 256),
    ]
    assert listed.json()["usage"]["prompt_tokens"] == 3
    assert tokens.json()["data"][0]["embedding"] == encode(embed([5, 6, 7]), "float")
    assert tokens.json()["usage"]["prompt_tokens"] == 3
    assert len(token_lists.json()["data"]) == 2
    assert token_lists.json()["usage"]["prompt_tokens"] == 3
    assert packed.json()["data"][0]["embedding"] == encode(embed("hello"), "base64")
    assert [record.to_json() for record in state.llm.embedding_requests][1] == {
        "id": "embreq_2",
        "received_at": state.llm.embedding_requests[1].received_at,
        "model": MODEL,
        "inputs": 2,
        "dimensions": 256,
        "encoding_format": "float",
        "status": 200,
        "fault": None,
    }


@pytest.mark.parametrize(
    ("body", "param", "code"),
    [
        ({"input": "x"}, "model", "missing_required_parameter"),
        ({"model": MODEL}, "input", "missing_required_parameter"),
        ({"model": MODEL, "input": ""}, "input", "invalid_value"),
        ({"model": MODEL, "input": ["a", ""]}, "input", "invalid_value"),
        ({"model": MODEL, "input": []}, "input", "invalid_value"),
        ({"model": MODEL, "input": ["a"] * 2049}, "input", "invalid_value"),
        ({"model": MODEL, "input": "x", "dimensions": 0}, "dimensions", "invalid_value"),
        ({"model": MODEL, "input": "x", "dimensions": 5000}, "dimensions", "invalid_value"),
        ({"model": MODEL, "input": "x", "encoding_format": "hex"}, "encoding_format", "invalid_value"),
    ],
)
async def test_validation_errors(body: dict[str, Any], param: str, code: str) -> None:
    state = llm_state()
    async with llm_client(state) as client:
        response = await client.post("/v1/embeddings", json=body)
    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["param"], error["code"], error["type"]) == (param, code, "invalid_request_error")
    assert state.llm.embedding_requests == []


async def test_auth_body_and_model_errors() -> None:
    state = llm_state()
    async with llm_client(state, key=None) as client:
        missing = await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        wrong = await client.post(
            "/v1/embeddings", json={"model": MODEL, "input": "x"}, headers={"authorization": "Bearer nope"}
        )
        not_json = await client.post("/v1/embeddings", content=b"[1]", headers=AUTH)
        unknown = await client.post("/v1/embeddings", json={"model": "nope", "input": "x"}, headers=AUTH)
    assert (missing.status_code, wrong.status_code, not_json.status_code) == (401, 401, 400)
    assert wrong.json()["error"]["code"] == "invalid_api_key"
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "model_not_found")
    assert [record.status for record in state.llm.embedding_requests] == [404]


async def test_request_log_control_reset_and_state() -> None:
    state = llm_state()
    async with llm_client(state) as client:
        await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        logged = (await client.get("/sim/llm/embeddings")).json()["requests"]
        counts = state.snapshot()["llm"]
        assert isinstance(counts, dict)
        assert counts["embeddings"] == 1
        assert (await client.delete("/sim/llm/embeddings")).status_code == 204
        assert (await client.get("/sim/llm/embeddings")).json() == {"requests": []}
        await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        assert (await client.post("/sim/reset", json={})).status_code in {200, 204}
    assert [entry["id"] for entry in logged] == ["embreq_1"]
    assert state.llm.embedding_requests == []


async def test_embedding_faults_fire_only_on_embeddings() -> None:
    state = llm_state()
    async with llm_client(state) as client:
        armed = await client.put(
            "/sim/llm/fault", json={"kind": "rate_limit", "retry_after_s": 2, "endpoint": "embeddings"}
        )
        chat = await client.post(
            "/v1/chat/completions", json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
        )
        limited = await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        await client.put(
            "/sim/llm/fault", json={"kind": "server_error", "status": 503, "endpoint": "embeddings"}
        )
        unavailable = await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        await client.put("/sim/llm/fault", json={"kind": "server_error"})
        untouched = await client.post("/v1/embeddings", json={"model": MODEL, "input": "x"})
        assert (await client.get("/sim/llm/fault")).json()["fault"]["endpoint"] == "chat"
    assert armed.status_code == 204
    assert chat.status_code == 200
    assert (limited.status_code, limited.headers["retry-after"]) == (429, "2")
    assert unavailable.status_code == 503
    assert untouched.status_code == 200
    assert [(record.fault, record.status) for record in state.llm.embedding_requests] == [
        ("rate_limit", 429),
        ("server_error", 503),
        (None, 200),
    ]


@pytest.mark.parametrize(
    "fault",
    [
        {"kind": "disconnect", "endpoint": "embeddings"},
        {"kind": "malformed_tool_args", "endpoint": "embeddings"},
        {"kind": "timeout", "after_tokens": 2, "endpoint": "embeddings"},
        {"kind": "timeout", "endpoint": "images"},
    ],
)
async def test_invalid_embedding_faults(fault: dict[str, object]) -> None:
    async with llm_client(llm_state()) as client:
        response = await client.put("/sim/llm/fault", json=fault)
    assert response.status_code == 422


async def test_timeout_holds_until_reset() -> None:
    state = realtime_llm_state()
    async with (
        served_llm(state) as url,
        httpx.AsyncClient(base_url=url, headers=AUTH, timeout=10.0) as client,
    ):
        await client.put("/sim/llm/fault", json={"kind": "timeout", "endpoint": "embeddings"})
        pending = asyncio.create_task(client.post("/v1/embeddings", json={"model": MODEL, "input": "x"}))
        await wait_held(state, 1)
        await client.post("/sim/reset", json={})
        released = await pending
    assert released.status_code == 504
    assert released.json()["error"]["code"] == "timeout"
