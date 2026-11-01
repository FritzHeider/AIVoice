"""Chat-completions request bodies and response parsing (Decision P03-T13a rules 8, 11).

The gateway reads the raw response body itself (the SDK's lenient model construction would let a malformed
body through as half-filled objects): anything that is not a chat completion with at least one choice is an
:class:`va_llm.errors.LlmProtocolError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import orjson

from va_llm.errors import LlmProtocolError
from va_llm.models import ChatMessage, TokenUsage, ToolCall, ToolChoice, ToolDef
from va_llm.wire_fields import Invalid, read_object, read_text, read_usage

__all__ = ["ChatRequest", "ParsedCompletion", "parse_completion"]


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """What one ``complete`` call asks for, independent of the target model."""

    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolDef, ...]
    tool_choice: ToolChoice | None
    temperature: float
    max_tokens: int
    response_format: Mapping[str, Any] | None

    def body(self) -> dict[str, Any]:
        """The SDK keyword arguments other than ``model`` (no ``stream``: non-streaming)."""
        body: dict[str, Any] = {
            "messages": [message.wire() for message in self.messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.tools:
            body["tools"] = [tool.wire() for tool in self.tools]
        if self.tool_choice is not None:
            body["tool_choice"] = (
                self.tool_choice if isinstance(self.tool_choice, str) else self.tool_choice.wire()
            )
        if self.response_format is not None:
            body["response_format"] = dict(self.response_format)
        return body


@dataclass(frozen=True, slots=True)
class ParsedCompletion:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None
    usage: TokenUsage | None


def _tool_calls(value: object) -> tuple[ToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        msg = "message.tool_calls is not an array"
        raise Invalid(msg)
    calls = []
    for index, item in enumerate(value):
        where = f"message.tool_calls[{index}]"
        call = read_object(item, where)
        function = read_object(call.get("function"), f"{where}.function")
        calls.append(
            ToolCall(
                id=read_text(call.get("id"), f"{where}.id") or "",
                name=read_text(function.get("name"), f"{where}.function.name", required=True) or "",
                arguments_json=read_text(function.get("arguments"), f"{where}.function.arguments") or "",
            )
        )
    return tuple(calls)


def _parse(content: bytes) -> ParsedCompletion:
    try:
        document = orjson.loads(content)
    except orjson.JSONDecodeError:
        msg = "the body is not JSON"
        raise Invalid(msg) from None
    body = read_object(document, "the body")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        msg = "the completion has no choices"
        raise Invalid(msg)
    choice = read_object(choices[0], "choices[0]")
    message = read_object(choice.get("message"), "choices[0].message")
    return ParsedCompletion(
        content=read_text(message.get("content"), "message.content"),
        tool_calls=_tool_calls(message.get("tool_calls")),
        finish_reason=read_text(choice.get("finish_reason"), "choices[0].finish_reason"),
        usage=read_usage(body.get("usage")),
    )


def parse_completion(content: bytes, *, provider_id: str, model: str) -> ParsedCompletion:
    """The first choice of a chat-completion body; :class:`LlmProtocolError` naming what is wrong."""
    try:
        return _parse(content)
    except Invalid as exc:
        message = f"{provider_id} {model}: {exc}"
    raise LlmProtocolError(message, provider_id=provider_id, model=model)
