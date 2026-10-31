"""FakeLLM request parsing: OpenAI 400 bodies with field paths, unknown params, text helpers."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from llm_helpers import MODEL, chat_body
from starlette.responses import JSONResponse

from va_sim.llm.params import ChatRequest, forced_tool, last_user_text, message_text, parse_chat, system_text

WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}},
}


def parsed(body: dict[str, Any]) -> ChatRequest:
    result = parse_chat(orjson.dumps(body))
    assert isinstance(result, ChatRequest), result
    return result


def rejected(raw: bytes | dict[str, Any]) -> dict[str, Any]:
    result = parse_chat(raw if isinstance(raw, bytes) else orjson.dumps(raw))
    assert isinstance(result, JSONResponse)
    assert result.status_code == 400
    error: dict[str, Any] = orjson.loads(result.body)["error"]
    assert error["type"] == "invalid_request_error"
    assert set(error) == {"message", "type", "param", "code"}
    return error


@pytest.mark.parametrize("raw", [b"{nope", b"[1, 2]", b'"text"'])
def test_bodies_that_are_not_json_objects(raw: bytes) -> None:
    error = rejected(raw)
    assert (error["param"], error["code"]) == (None, None)
    assert "could not parse the JSON body" in error["message"]


def test_missing_parameters_name_the_field() -> None:
    error = rejected({"model": MODEL})
    assert error == {
        "message": "Missing required parameter: 'messages'.",
        "type": "invalid_request_error",
        "param": "messages",
        "code": "missing_required_parameter",
    }
    assert rejected({"messages": [{"role": "user", "content": "hi"}]})["param"] == "model"


@pytest.mark.parametrize(
    ("body", "param"),
    [
        (chat_body("hi") | {"messages": [{"role": "robot", "content": "x"}]}, "messages[0].role"),
        (chat_body("hi") | {"messages": []}, "messages"),
        (chat_body("hi", temperature=3), "temperature"),
        (chat_body("hi", max_tokens=0), "max_tokens"),
        (chat_body("hi", max_completion_tokens=-1), "max_completion_tokens"),
        (
            chat_body("hi", tools=[{"type": "function", "function": {"name": "bad name!"}}]),
            "tools[0].function.name",
        ),
        (chat_body("hi", tool_choice="sometimes"), "tool_choice"),
        (chat_body("hi", response_format={"type": "xml"}), "response_format.type"),
        (chat_body("hi") | {"messages": [{"role": "user", "content": 5}]}, "messages[0].content"),
    ],
)
def test_invalid_values_name_the_field(body: dict[str, Any], param: str) -> None:
    error = rejected(body)
    assert error["param"] == param
    assert error["code"] == "invalid_value"
    assert error["message"].startswith(f"Invalid value for '{param}': ")


@pytest.mark.parametrize(
    ("message", "fragment"),
    [
        ({"role": "user"}, "a user message needs content"),
        ({"role": "assistant"}, "an assistant message needs content or tool_calls"),
        ({"role": "tool", "content": "42"}, "a tool message needs tool_call_id"),
    ],
)
def test_message_rules(message: dict[str, Any], fragment: str) -> None:
    error = rejected(chat_body() | {"messages": [message]})
    assert error["param"] == "messages[0]"
    assert fragment in error["message"]


def test_n_must_be_one() -> None:
    assert rejected(chat_body("hi", n=2))["param"] == "n"
    assert parsed(chat_body("hi", n=1)).n == 1


def test_tool_choice_needs_tools_and_a_known_function() -> None:
    named = {"type": "function", "function": {"name": "book"}}
    assert rejected(chat_body("hi", tool_choice="required"))["param"] == "tool_choice"
    assert rejected(chat_body("hi", tool_choice=named))["param"] == "tool_choice"
    error = rejected(chat_body("hi", tools=[WEATHER_TOOL], tool_choice=named))
    assert (error["param"], error["code"]) == ("tool_choice", "invalid_value")
    assert "'book'" in error["message"]
    assert rejected(chat_body("hi", tool_choice={"type": "function"}))["param"] == "tool_choice"
    # "auto" and "none" without tools are accepted, as OpenAI-compatible servers commonly do.
    assert parsed(chat_body("hi", tool_choice="auto")).tool_choice == "auto"


def test_unknown_parameters_are_accepted_and_limits_resolve() -> None:
    request = parsed(
        chat_body("hi", seed=3, top_p=0.5, max_tokens=10, stream_options={"include_usage": True})
    )
    assert request.token_limit == 10
    assert request.include_usage
    assert parsed(chat_body("hi", max_tokens=10, max_completion_tokens=4)).token_limit == 4
    assert not parsed(chat_body("hi", stream=True)).include_usage


def test_text_helpers() -> None:
    request = parsed(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "developer", "content": [{"type": "text", "text": "[[SCENARIO:x]]"}]},
                {"role": "user", "content": [{"type": "text", "text": "first"}, {"type": "image_url"}]},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "there"}],
                },
            ],
            "tools": [WEATHER_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        }
    )
    assert system_text(request) == "You are helpful.\n[[SCENARIO:x]]"
    assert last_user_text(request) == "hello there"
    assert message_text(request.messages[3]) == ""
    assert forced_tool(request) == "get_weather"
    assert request.tool_names == ("get_weather",)
    assert last_user_text(parsed({"model": MODEL, "messages": [{"role": "system", "content": "s"}]})) is None
    assert forced_tool(parsed(chat_body("x", tools=[WEATHER_TOOL], tool_choice="required"))) is None
