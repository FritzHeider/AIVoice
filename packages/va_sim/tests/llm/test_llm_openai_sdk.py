"""Acceptance (specs/30 §10): the official ``openai`` SDK works against FakeLLM unmodified."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import openai
import pytest
from llm_helpers import API_KEY, MODEL, realtime_llm_state, served_llm
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from va_sim.llm.rules import RUNTIME_SOURCE, load_rules
from va_sim.settings import DEFAULT_LLM_MODELS
from va_sim.state import SimState

MESSAGES: list[ChatCompletionMessageParam] = [
    {"role": "system", "content": "[[SCENARIO:restaurant]]"},
    {"role": "user", "content": "I want to book a table"},
]
TOOLS: list[ChatCompletionToolParam] = [
    {
        "type": "function",
        "function": {
            "name": "book_table",
            "parameters": {
                "type": "object",
                "properties": {"party": {"type": "integer"}, "time": {"type": "string"}},
            },
        },
    }
]
RULES = """\
rules:
  - id: reply
    match: {system_contains: ["[[SCENARIO:restaurant]]"], tool_name: null}
    respond: {content: "Sure! For how many people?"}
  - id: book
    priority: 5
    match: {tool_name: book_table}
    respond:
      tool_calls: [{name: book_table, arguments: {party: 4, time: "19:00", note: "window seat, please"}}]
"""


def sim_with_rules() -> SimState:
    state = realtime_llm_state()
    state.llm.rules.add(load_rules(RULES).rules, source=RUNTIME_SOURCE)
    return state


@asynccontextmanager
async def sdk_client(state: SimState, api_key: str = API_KEY) -> AsyncIterator[openai.AsyncOpenAI]:
    async with served_llm(state) as url:
        client = openai.AsyncOpenAI(base_url=f"{url}/v1", api_key=api_key, max_retries=0, timeout=10.0)
        try:
            yield client
        finally:
            await client.close()


async def test_non_streaming_text() -> None:
    async with sdk_client(sim_with_rules()) as client:
        completion = await client.chat.completions.create(model=MODEL, messages=MESSAGES)
    choice = completion.choices[0]
    assert choice.message.content == "Sure! For how many people?"
    assert choice.finish_reason == "stop"
    assert completion.usage is not None
    assert completion.usage.completion_tokens == 5


async def test_streaming_text_with_usage() -> None:
    async with sdk_client(sim_with_rules()) as client:
        stream = await client.chat.completions.create(
            model=MODEL, messages=MESSAGES, stream=True, stream_options={"include_usage": True}
        )
        parts: list[str] = []
        finish: list[str] = []
        usage = None
        async for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage
            for choice in chunk.choices:
                parts.append(choice.delta.content or "")
                if choice.finish_reason is not None:
                    finish.append(choice.finish_reason)
    assert "".join(parts) == "Sure! For how many people?"
    assert finish == ["stop"]
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens) == (7, 5)


async def test_streamed_tool_call_deltas_assemble() -> None:
    async with sdk_client(sim_with_rules()) as client:
        stream = await client.chat.completions.create(
            model=MODEL, messages=MESSAGES, tools=TOOLS, stream=True
        )
        calls: dict[int, dict[str, str]] = {}
        finish = None
        async for chunk in stream:
            for choice in chunk.choices:
                finish = choice.finish_reason or finish
                for delta in choice.delta.tool_calls or ():
                    call = calls.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
                    call["id"] += delta.id or ""
                    if delta.function is not None:
                        call["name"] += delta.function.name or ""
                        call["arguments"] += delta.function.arguments or ""
    assert finish == "tool_calls"
    (call,) = calls.values()
    assert call["id"].startswith("call_")
    assert call["name"] == "book_table"
    assert json.loads(call["arguments"]) == {"party": 4, "time": "19:00", "note": "window seat, please"}


async def test_stream_helper_and_forced_tool_choice() -> None:
    async with (
        sdk_client(sim_with_rules()) as client,
        client.chat.completions.stream(
            model=MODEL,
            messages=MESSAGES,
            tools=TOOLS,
            tool_choice={"type": "function", "function": {"name": "book_table"}},
        ) as stream,
    ):
        final = await stream.get_final_completion()
    (tool_call,) = final.choices[0].message.tool_calls or []
    assert tool_call.type == "function"
    assert tool_call.function.name == "book_table"
    assert json.loads(tool_call.function.arguments)["party"] == 4
    assert final.choices[0].finish_reason == "tool_calls"


async def test_models_list() -> None:
    async with sdk_client(realtime_llm_state()) as client:
        page = await client.models.list()
    assert [model.id for model in page.data] == list(DEFAULT_LLM_MODELS)


async def test_errors_map_to_sdk_exceptions() -> None:
    state = realtime_llm_state()
    async with sdk_client(state) as client:
        state.llm.arm_fault("rate_limit", times=1, retry_after_s=1)
        with pytest.raises(openai.RateLimitError) as limited:
            await client.chat.completions.create(model=MODEL, messages=MESSAGES)
        assert limited.value.response.headers["retry-after"] == "1"
        with pytest.raises(openai.NotFoundError):
            await client.chat.completions.create(model="acme/nope", messages=MESSAGES)
        with pytest.raises(openai.BadRequestError) as invalid:
            await client.chat.completions.create(model=MODEL, messages=MESSAGES, temperature=5)
        assert invalid.value.param == "temperature"
        state.llm.arm_fault("server_error", times=1, status=502)
        with pytest.raises(openai.InternalServerError):
            await client.chat.completions.create(model=MODEL, messages=MESSAGES)
    async with sdk_client(realtime_llm_state(), api_key="wrong") as client:
        with pytest.raises(openai.AuthenticationError):
            await client.chat.completions.create(model=MODEL, messages=MESSAGES)
