"""FakeLLM ``/v1/chat/completions`` and ``/v1/models`` in-process: bodies, SSE, auth, models, log, pacing."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from llm_helpers import (
    DONE,
    MODEL,
    add_rules,
    chat_body,
    deltas,
    llm_client,
    llm_state,
    sse_events,
    streamed_text,
)

from va_sim.llm.rules import RUNTIME_SOURCE, load_rules
from va_sim.settings import DEFAULT_LLM_MODELS
from va_sim.state import SimState

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "book", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "lookup"}},
]
BOOK_RULE = """\
rules:
  - id: book
    match: {last_user_regex: "(?i)book"}
    respond:
      tool_calls:
        - name: book
          arguments: {party: 4, time: "19:00"}
"""


async def test_non_stream_body() -> None:
    async with llm_client(llm_state()) as client:
        response = await client.post("/v1/chat/completions", json=chat_body("hello there"))
    assert response.status_code == 200
    body = response.json()
    assert body["id"].startswith("chatcmpl-")
    assert len(body["id"]) == len("chatcmpl-") + 29
    assert (body["object"], body["model"], body["system_fingerprint"]) == ("chat.completion", MODEL, "fp_sim")
    assert isinstance(body["created"], int)
    assert body["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Okay. You said: hello there.", "refusal": None},
            "logprobs": None,
            "finish_reason": "stop",
        }
    ]
    assert body["usage"] == {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7}


async def test_stream_wire_format() -> None:
    async with llm_client(llm_state()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=chat_body("hello there", stream=True, stream_options={"include_usage": True}),
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.endswith("data: [DONE]\n\n")
    events = sse_events(response.text)
    assert events[-1] == DONE
    chunks = [event for event in events if isinstance(event, dict)]
    assert len({chunk["id"] for chunk in chunks}) == 1
    assert deltas(events)[0] == {"role": "assistant", "content": ""}
    assert [delta.get("content") for delta in deltas(events)[1:-1]] == [
        "Okay. ",
        "You ",
        "said: ",
        "hello ",
        "there.",
    ]
    assert streamed_text(events) == "Okay. You said: hello there."
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert all(chunk["usage"] is None for chunk in chunks[:-1])
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"] == {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7}


async def test_stream_without_usage_has_no_usage_fields() -> None:
    async with llm_client(llm_state()) as client:
        response = await client.post("/v1/chat/completions", json=chat_body("hi", stream=True))
    chunks = [event for event in sse_events(response.text) if isinstance(event, dict)]
    assert all("usage" not in chunk for chunk in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


async def test_streamed_tool_calls_assemble() -> None:
    async with llm_client(llm_state()) as client:
        await add_rules(client, BOOK_RULE)
        response = await client.post(
            "/v1/chat/completions", json=chat_body("Book it", tools=TOOLS, stream=True)
        )
    events = sse_events(response.text)
    calls: dict[int, dict[str, str]] = {}
    for delta in deltas(events):
        for part in delta.get("tool_calls", []):
            call = calls.setdefault(part["index"], {"id": "", "name": "", "arguments": ""})
            call["id"] += part.get("id", "")
            call["name"] += part.get("function", {}).get("name", "")
            call["arguments"] += part["function"]["arguments"]
    (call,) = calls.values()
    assert call["id"].startswith("call_")
    assert len(call["id"]) == len("call_") + 24
    assert call["name"] == "book"
    assert orjson.loads(call["arguments"]) == {"party": 4, "time": "19:00"}
    finish = [event for event in events if isinstance(event, dict)][-1]
    assert finish["choices"][0]["finish_reason"] == "tool_calls"
    assert streamed_text(events) == ""


async def test_non_stream_tool_call_and_forced_choice() -> None:
    async with llm_client(llm_state()) as client:
        await add_rules(client, BOOK_RULE)
        ruled = (await client.post("/v1/chat/completions", json=chat_body("book", tools=TOOLS))).json()
        forced = await client.post(
            "/v1/chat/completions",
            json=chat_body(
                "hi", tools=TOOLS, tool_choice={"type": "function", "function": {"name": "lookup"}}
            ),
        )
    message = ruled["choices"][0]["message"]
    assert message["content"] is None
    assert message["tool_calls"][0]["function"] == {"name": "book", "arguments": '{"party":4,"time":"19:00"}'}
    assert ruled["choices"][0]["finish_reason"] == "tool_calls"
    forced_call = forced.json()["choices"][0]["message"]["tool_calls"][0]
    assert forced_call["function"] == {"name": "lookup", "arguments": "{}"}


async def test_max_tokens_finishes_with_length() -> None:
    async with llm_client(llm_state()) as client:
        response = await client.post("/v1/chat/completions", json=chat_body("one two three", max_tokens=2))
    choice = response.json()["choices"][0]
    assert (choice["message"]["content"], choice["finish_reason"]) == ("Okay. You ", "length")
    assert response.json()["usage"]["completion_tokens"] == 2


@pytest.mark.parametrize(
    ("headers", "code", "fragment"),
    [
        ({}, None, "didn't provide an API key"),
        ({"authorization": "Basic abc"}, None, "didn't provide an API key"),
        ({"authorization": "Bearer wrong"}, "invalid_api_key", "Incorrect API key"),
    ],
)
async def test_authentication(headers: dict[str, str], code: str | None, fragment: str) -> None:
    async with llm_client(llm_state(), key=None) as client:
        for path, method in (("/v1/chat/completions", "POST"), ("/v1/models", "GET")):
            response = await client.request(method, path, json=chat_body("hi"), headers=headers)
            assert response.status_code == 401
            error = response.json()["error"]
            assert (error["type"], error["code"], error["param"]) == ("invalid_request_error", code, None)
            assert fragment in error["message"]


async def test_unknown_model_and_added_keys_and_models() -> None:
    state = llm_state()
    async with llm_client(state, key=None) as client:
        missing = await client.post(
            "/v1/chat/completions",
            json=chat_body("hi") | {"model": "acme/custom"},
            headers={"authorization": "Bearer sim-llm-key"},
        )
        assert missing.status_code == 404
        assert missing.json()["error"] == {
            "message": "The model `acme/custom` does not exist or you do not have access to it.",
            "type": "invalid_request_error",
            "param": "model",
            "code": "model_not_found",
        }
        assert (await client.post("/sim/llm/api-keys", json={"api_key": "org-key"})).status_code == 204
        assert (await client.post("/sim/llm/models", json={"id": "acme/custom"})).status_code == 204
        ok = await client.post(
            "/v1/chat/completions",
            json=chat_body("hi") | {"model": "acme/custom"},
            headers={"authorization": "Bearer org-key"},
        )
        assert ok.status_code == 200
        assert ok.json()["model"] == "acme/custom"
        listed = await client.get("/v1/models", headers={"authorization": "Bearer org-key"})
    assert [model["id"] for model in listed.json()["data"]] == [*DEFAULT_LLM_MODELS, "acme/custom"]
    assert listed.json()["object"] == "list"
    assert listed.json()["data"][0] == {
        "id": DEFAULT_LLM_MODELS[0],
        "object": "model",
        "created": listed.json()["data"][0]["created"],
        "owned_by": "sim",
    }
    assert [record.status for record in state.llm.requests] == [404, 200]


async def test_request_log() -> None:
    state = llm_state()
    async with llm_client(state) as client:
        await add_rules(client, BOOK_RULE)
        await client.post(
            "/v1/chat/completions",
            json=chat_body(
                "please book",
                system="[[SCENARIO:x]]",
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
                max_completion_tokens=50,
                response_format={"type": "text"},
                stream=True,
            ),
            headers={"x-va-call-id": "call-7"},
        )
        await client.post("/v1/chat/completions", json=chat_body("hi"))
        log = (await client.get("/sim/llm/requests")).json()["requests"]
        assert (await client.delete("/sim/llm/requests")).status_code == 204
        assert (await client.get("/sim/llm/requests")).json() == {"requests": []}
    first, second = log
    assert first == {
        "id": "llmreq_1",
        "received_at": first["received_at"],
        "model": MODEL,
        "call_id": "call-7",
        "stream": True,
        "messages": [
            {"role": "system", "content": "[[SCENARIO:x]]"},
            {"role": "user", "content": "please book"},
        ],
        "tools": [
            {"type": "function", "function": {"name": "book", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "lookup"}},
        ],
        "tool_choice": "auto",
        "temperature": 0.2,
        "max_tokens": 50,
        "response_format": {"type": "text"},
        "rule_id": "book",
        "fault": None,
        "status": 200,
        "finish_reason": "tool_calls",
    }
    assert first["received_at"].endswith("Z")
    assert (second["id"], second["call_id"], second["rule_id"], second["finish_reason"]) == (
        "llmreq_2",
        None,
        None,
        "stop",
    )


async def test_call_index_sequences_replies_per_call() -> None:
    rules = """\
rules:
  - id: turn-0
    match: {call_index: 0}
    respond: {content: "First."}
  - id: turn-1
    match: {call_index: 1}
    respond: {content: "Second."}
"""
    async with llm_client(llm_state()) as client:
        await add_rules(client, rules)
        replies = []
        for call in ("a", "b", "a", "b", "a"):
            response = await client.post(
                "/v1/chat/completions", json=chat_body("hi"), headers={"x-va-call-id": call}
            )
            replies.append(response.json()["choices"][0]["message"]["content"])
    assert replies == ["First.", "First.", "Second.", "Second.", "Okay. You said: hi."]


async def elapsed_ms(state: SimState, body: dict[str, Any]) -> int:
    async with llm_client(state) as client:
        start = state.clock.virtual_ms()
        response = await client.post("/v1/chat/completions", json=body)
        assert response.status_code == 200
        return state.clock.virtual_ms() - start


@pytest.mark.parametrize("stream", [False, True])
async def test_pacing_on_the_sim_clock(stream: bool) -> None:
    # 5 tokens: ttft 150 ms + 4 intervals of 12.5 ms.
    assert (
        await elapsed_ms(llm_state(ttft_ms=150, tokens_per_s=80.0), chat_body("hello there", stream=stream))
        == 200
    )


async def test_rule_timing_overrides() -> None:
    state = llm_state(ttft_ms=150, tokens_per_s=100.0)
    rules = load_rules('rules:\n  - id: slow\n    respond: {content: "a b c", ttft_ms: 40, delay_ms: 60}\n')
    state.llm.rules.add(rules.rules, source=RUNTIME_SOURCE)
    # 3 tokens: 40 + 60 ms before the first, then 2 x 10 ms.
    assert await elapsed_ms(state, chat_body("x")) == 120
