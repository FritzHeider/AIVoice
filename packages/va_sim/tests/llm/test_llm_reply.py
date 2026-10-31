"""FakeLLM replies and wire builders: tokens, default replies, tool calls, truncation, usage, chunks."""

from __future__ import annotations

import itertools
from typing import Any

import orjson
import pytest
from llm_helpers import MODEL, chat_body

from va_sim.llm.params import ChatRequest
from va_sim.llm.reply import (
    GREETING,
    OutToolCall,
    Reply,
    default_text,
    malformed,
    plan_reply,
    tokens,
    truncate,
    usage,
)
from va_sim.llm.rules import RuleResponse, RuleToolCall
from va_sim.llm.wire import DONE, Meta, completion_body, paced_count, split_arguments, sse, stream_emissions

TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "first_tool"}},
    {"type": "function", "function": {"name": "second_tool"}},
]
META = Meta(id="chatcmpl-x", created=1_700_000_000, model=MODEL)


def request(*texts: str, **extra: Any) -> ChatRequest:
    return ChatRequest.model_validate(chat_body(*texts, **extra))


def plan(req: ChatRequest, respond: RuleResponse | None = None) -> Reply:
    counter = itertools.count(1)
    return plan_reply(req, respond, new_call_id=lambda: f"call_{next(counter)}")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hi  there\n", ("Hi  ", "there\n")),
        ("  lead in", ("  lead ", "in")),
        ("one", ("one",)),
        ("", ()),
        ("   ", ("   ",)),
    ],
)
def test_tokens_keep_trailing_whitespace(text: str, expected: tuple[str, ...]) -> None:
    assert tokens(text) == expected
    assert "".join(tokens(text)) == text


def test_default_replies() -> None:
    assert default_text("  book a table  ") == "Okay. You said: book a table."
    assert default_text(None) == GREETING
    reply = plan(request("hello there"))
    assert (reply.content, reply.tool_calls, reply.finish_reason) == (
        "Okay. You said: hello there.",
        (),
        "stop",
    )
    assert plan(ChatRequest.model_validate(chat_body(system="sys"))).content == GREETING


def test_forced_and_required_tool_choice_without_a_rule_call_with_empty_arguments() -> None:
    forced = plan(
        request("x", tools=TOOLS, tool_choice={"type": "function", "function": {"name": "second_tool"}})
    )
    assert forced.tool_calls == (OutToolCall("call_1", "second_tool", "{}"),)
    assert (forced.content, forced.finish_reason) == (None, "tool_calls")
    required = plan(request("x", tools=TOOLS, tool_choice="required"))
    assert required.tool_calls[0].name == "first_tool"
    # "auto" with tools and no rule answers text.
    assert plan(request("x", tools=TOOLS, tool_choice="auto")).tool_calls == ()


def test_rule_replies() -> None:
    respond = RuleResponse(
        content="Booking now.",
        tool_calls=(
            RuleToolCall(name="book", arguments={"party": 4}),
            RuleToolCall(name="raw", arguments="{bad"),
        ),
    )
    reply = plan(request("x", tools=TOOLS), respond)
    assert reply.content == "Booking now."
    assert reply.tool_calls == (
        OutToolCall("call_1", "book", '{"party":4}'),
        OutToolCall("call_2", "raw", "{bad"),
    )
    assert reply.finish_reason == "tool_calls"
    # tool_choice "none" drops every tool call, a rule's included.
    assert plan(request("x", tools=TOOLS, tool_choice="none"), respond).tool_calls == ()
    # A rule with neither content nor tool calls (timing only) answers the default reply.
    assert plan(request("hi"), RuleResponse(ttft_ms=5)).content == "Okay. You said: hi."
    assert plan(request("hi"), RuleResponse(content="")).content == ""


def test_truncation() -> None:
    reply = Reply(("a ", "b ", "c"), (OutToolCall("call_1", "f", "{}"),), "tool_calls")
    assert truncate(reply, None) is reply
    assert truncate(reply, 10) is reply
    cut = truncate(reply, 2)
    assert (cut.tokens, cut.tool_calls, cut.finish_reason) == (("a ", "b "), (), "length")
    no_tool = truncate(reply, 3)
    assert (no_tool.tokens, no_tool.tool_calls, no_tool.finish_reason) == (("a ", "b ", "c"), (), "length")
    assert truncate(reply, 5) is reply  # the call costs 1 + one argument word


def test_malformed_arguments_are_invalid_json() -> None:
    reply = malformed(
        Reply((), (OutToolCall("call_1", "f", '{"party":4}'), OutToolCall("c", "g", "{}")), "tool_calls")
    )
    assert [call.arguments for call in reply.tool_calls] == ['{"par', "{"]
    for call in reply.tool_calls:
        with pytest.raises(orjson.JSONDecodeError):
            orjson.loads(call.arguments)


def test_usage_counts_words() -> None:
    req = ChatRequest.model_validate(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "be brief"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c", "type": "function", "function": {"name": "f", "arguments": '{"a": 1}'}}
                    ],
                },
                {"role": "user", "content": "three words here"},
            ],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        }
    )
    reply = Reply(("x ", "y"), (OutToolCall("call_1", "f", '{"a": 1}'),), "tool_calls")
    counted = usage(req, reply)
    # 2 + 2 (arguments) + 3 + tool JSON words ('[{"type": "function", "function": {"name": "f"}}]' = 5)
    assert counted.prompt_tokens == 12
    assert counted.completion_tokens == 2 + 1 + 2
    assert counted.to_json() == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


def test_split_arguments() -> None:
    assert split_arguments("") == [""]
    assert split_arguments("{}") == ["{", "}"]
    parts = split_arguments('{"party": 4, "time": "19:00"}')
    assert len(parts) == 3
    assert "".join(parts) == '{"party": 4, "time": "19:00"}'


def test_stream_emissions() -> None:
    reply = Reply(("Hi ", "there"), (OutToolCall("call_1", "f", '{"a":1}'),), "tool_calls")
    counted = usage(request("x"), reply)
    emissions = stream_emissions(META, reply, counted, include_usage=True)
    chunks: list[Any] = [emission.chunk for emission in emissions]
    assert all(chunk["id"] == "chatcmpl-x" and chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert all(chunk["model"] == MODEL and chunk["system_fingerprint"] == "fp_sim" for chunk in chunks)
    assert all(chunk["usage"] is None for chunk in chunks[:-1])
    assert chunks[0]["choices"] == [
        {"index": 0, "delta": {"role": "assistant", "content": ""}, "logprobs": None, "finish_reason": None}
    ]
    assert [emission.paced for emission in emissions] == [
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        False,
    ]
    assert chunks[3]["choices"][0]["delta"] == {
        "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": ""}}
        ]
    }
    assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"] == counted.to_json()
    assert paced_count(reply) == 6
    without = stream_emissions(META, reply, counted, include_usage=False)
    assert all("usage" not in emission.chunk for emission in without)
    assert len(without) == len(emissions) - 1
    assert sse({"a": 1}) == b'data: {"a":1}\n\n'
    assert DONE == b"data: [DONE]\n\n"


def test_completion_body() -> None:
    text = Reply(("Hi",), (), "stop")
    body: Any = completion_body(META, text, usage(request("x"), text))
    assert body["object"] == "chat.completion"
    assert body["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hi", "refusal": None},
            "logprobs": None,
            "finish_reason": "stop",
        }
    ]
    tools = Reply((), (OutToolCall("call_1", "f", "{}"),), "tool_calls")
    tool_body: Any = completion_body(META, tools, usage(request("x"), tools))
    message = tool_body["choices"][0]["message"]
    assert message["content"] is None
    assert message["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    ]
