"""Chat-completion body parsing (Decision P03-T13a rule 8)."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from llm_client_helpers import completion_body

from va_llm import LlmProtocolError, TokenUsage, ToolCall
from va_llm.wire import parse_completion


def _parse(body: Any) -> Any:
    content = body if isinstance(body, bytes) else orjson.dumps(body)
    return parse_completion(content, provider_id="platform", model="m")


def test_content_tool_calls_finish_reason_and_usage() -> None:
    calls = [
        {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        {"type": "function", "function": {"name": "g"}},
    ]
    parsed = _parse(completion_body("Hi", tool_calls=calls, usage=(3, 4), finish_reason="tool_calls"))
    assert parsed.content == "Hi"
    assert parsed.tool_calls == (
        ToolCall(id="a", name="f", arguments_json="{}"),
        ToolCall(id="", name="g", arguments_json=""),
    )
    assert parsed.finish_reason == "tool_calls"
    assert parsed.usage == TokenUsage(prompt_tokens=3, completion_tokens=4)


def test_absent_or_null_usage_and_content() -> None:
    body = completion_body(None, usage=None)
    body["choices"][0]["finish_reason"] = None
    parsed = _parse(body)
    assert (parsed.content, parsed.tool_calls, parsed.finish_reason, parsed.usage) == (None, (), None, None)
    body["usage"] = None
    body["choices"][0]["message"]["tool_calls"] = None
    assert _parse(body).usage is None


def _with(path: str, value: Any) -> dict[str, Any]:
    body = completion_body(
        tool_calls=[{"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    )
    target: Any = body
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[int(key)] if key.isdigit() else target[key]
    last = keys[-1]
    target[int(last) if last.isdigit() else last] = value
    return body


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (b"not json", "the body is not JSON"),
        ([], "the body is not an object"),
        ({"choices": []}, "no choices"),
        ({"choices": "x"}, "no choices"),
        ({}, "no choices"),
        (_with("choices.0", "x"), r"choices\[0\] is not an object"),
        (_with("choices.0.message", None), r"choices\[0\].message is not an object"),
        (_with("choices.0.message.content", 5), "message.content is not a string"),
        (_with("choices.0.finish_reason", 1), "finish_reason is not a string"),
        (_with("choices.0.message.tool_calls", {}), "tool_calls is not an array"),
        (_with("choices.0.message.tool_calls.0", "x"), r"tool_calls\[0\] is not an object"),
        (
            _with("choices.0.message.tool_calls.0.function", None),
            r"tool_calls\[0\].function is not an object",
        ),
        (_with("choices.0.message.tool_calls.0.function.name", ""), "name is not a non-empty string"),
        (_with("choices.0.message.tool_calls.0.function.arguments", {"a": 1}), "arguments is not a string"),
        (_with("choices.0.message.tool_calls.0.id", 7), r"tool_calls\[0\].id is not a string"),
        (_with("usage", "x"), "usage is not an object"),
        (_with("usage.prompt_tokens", -1), "prompt_tokens is not a non-negative integer"),
        (_with("usage.completion_tokens", True), "completion_tokens is not a non-negative integer"),
        (_with("usage.prompt_tokens", 1.5), "prompt_tokens is not a non-negative integer"),
    ],
)
def test_malformed_bodies_are_protocol_errors(body: Any, match: str) -> None:
    with pytest.raises(LlmProtocolError, match=match) as caught:
        _parse(body)
    assert caught.value.error_kind == "parse"
    assert (caught.value.provider_id, caught.value.model) == ("platform", "m")
