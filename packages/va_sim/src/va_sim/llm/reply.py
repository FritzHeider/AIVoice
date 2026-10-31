"""What FakeLLM answers (specs/30 §3.4 default dialogue response; Decision P03-T06a rules 5, 8).

Pure: the reply text is split into tokens (whitespace-split words with their trailing whitespace); tool
calls come from the winning rule or, for a forced/required ``tool_choice`` without a rule, call the forced (or
first) tool with ``{}``. ``max_tokens`` truncates; usage counts whitespace words.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final, Literal

import orjson
from pydantic import JsonValue

from va_sim.llm.params import ChatRequest, forced_tool, last_user_text, message_text
from va_sim.llm.rules import RuleResponse, RuleToolCall

__all__ = [
    "GREETING",
    "FinishReason",
    "OutToolCall",
    "Reply",
    "Usage",
    "default_text",
    "malformed",
    "plan_reply",
    "tokens",
    "truncate",
    "usage",
    "word_count",
]

type FinishReason = Literal["stop", "tool_calls", "length"]

GREETING: Final = "Hello! How can I help you today?"
_TOKEN: Final = re.compile(r"\S+\s*")
_LEADING_SPACE: Final = re.compile(r"^\s*")


@dataclass(frozen=True, slots=True)
class OutToolCall:
    id: str
    name: str
    arguments: str

    @property
    def cost(self) -> int:
        """Completion tokens: one for the call plus the argument JSON's words."""
        return 1 + word_count(self.arguments)


@dataclass(frozen=True, slots=True)
class Reply:
    tokens: tuple[str, ...]
    tool_calls: tuple[OutToolCall, ...]
    finish_reason: FinishReason

    @property
    def content(self) -> str | None:
        """The text, or None when the reply is tool calls only (OpenAI's ``content: null``)."""
        if not self.tokens and self.tool_calls:
            return None
        return "".join(self.tokens)


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def word_count(text: str) -> int:
    return len(text.split())


def tokens(text: str) -> tuple[str, ...]:
    """Words with their trailing whitespace; leading whitespace is glued to the first token."""
    parts = _TOKEN.findall(text)
    leading = _LEADING_SPACE.match(text)
    prefix = leading.group(0) if leading is not None else ""
    if not parts:
        return (prefix,) if prefix else ()
    return (prefix + parts[0], *parts[1:])


def default_text(last_user: str | None) -> str:
    """``Okay. You said: {last user message}.``, or the greeting when there is no user message."""
    if last_user is None:
        return GREETING
    return f"Okay. You said: {last_user.strip()}."


def _arguments(call: RuleToolCall) -> str:
    if isinstance(call.arguments, str):
        return call.arguments
    return orjson.dumps(call.arguments).decode("utf-8")


def _default_reply(request: ChatRequest) -> tuple[str, list[tuple[str, str]]]:
    """A call of the named (or, for ``required``, the first) tool with ``{}``, else the default text."""
    forced = forced_tool(request)
    if forced is None and request.tool_choice == "required" and request.tool_names:
        forced = request.tool_names[0]
    if forced is not None:
        return "", [(forced, "{}")]
    return default_text(last_user_text(request)), []


def plan_reply(
    request: ChatRequest, respond: RuleResponse | None, *, new_call_id: Callable[[], str]
) -> Reply:
    """The reply before truncation: the rule's content/tool calls, else the default; ``tool_choice: none``
    drops every tool call. A rule with neither content nor tool calls answers the default reply."""
    if respond is not None and (respond.content is not None or respond.tool_calls):
        text = respond.content or ""
        calls = [(call.name, _arguments(call)) for call in respond.tool_calls]
    else:
        text, calls = _default_reply(request)
    if request.tool_choice == "none":
        calls = []
    out = tuple(OutToolCall(new_call_id(), name, arguments) for name, arguments in calls)
    return Reply(tokens(text), out, "tool_calls" if out else "stop")


def truncate(reply: Reply, limit: int | None) -> Reply:
    """Cut ``reply`` to ``limit`` completion tokens: content first, then whole tool calls that still fit;
    any cut makes ``finish_reason`` ``length``."""
    if limit is None:
        return reply
    kept_tokens = reply.tokens[:limit]
    remaining = limit - len(kept_tokens)
    kept_calls: list[OutToolCall] = []
    for call in reply.tool_calls:
        if call.cost > remaining:
            break
        kept_calls.append(call)
        remaining -= call.cost
    if len(kept_tokens) == len(reply.tokens) and len(kept_calls) == len(reply.tool_calls):
        return reply
    return Reply(kept_tokens, tuple(kept_calls), "length")


def malformed(reply: Reply) -> Reply:
    """Each tool call's arguments cut to their first half, invalid JSON (``malformed_tool_args`` fault)."""
    calls = tuple(
        replace(call, arguments=call.arguments[: len(call.arguments) // 2]) for call in reply.tool_calls
    )
    return replace(reply, tool_calls=calls)


def usage(request: ChatRequest, reply: Reply) -> Usage:
    """Prompt tokens: words of every message (text and tool-call arguments) plus the tool definitions' JSON;
    completion tokens: content tokens plus each tool call's cost."""
    prompt = 0
    for message in request.messages:
        prompt += word_count(message_text(message))
        prompt += sum(word_count(call.function.arguments) for call in message.tool_calls or ())
    if request.tools:
        prompt += word_count(
            json.dumps([tool.model_dump(mode="json", exclude_none=True) for tool in request.tools])
        )
    completion = len(reply.tokens) + sum(call.cost for call in reply.tool_calls)
    return Usage(prompt_tokens=prompt, completion_tokens=completion)
