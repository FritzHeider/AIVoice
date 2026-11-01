"""Server-sent chat-completion chunks (specs/10-llm-gateway.md §2; Decision P03-T13b rule 3).

The gateway reads the raw SSE lines of a streaming completion itself: ``data: <json>`` lines carry chunks,
``data: [DONE]`` ends the stream, blank lines, ``:`` comments, and other SSE fields are ignored.
:class:`StreamAssembler` turns the chunks into content deltas and accumulates tool calls by ``index``,
the last non-null ``finish_reason``, and the trailing ``usage``. Reasoning deltas (``reasoning_content``,
``reasoning``) and role-only deltas are not tokens. Anything malformed is an :class:`LlmProtocolError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import orjson

from va_llm.errors import LlmProtocolError
from va_llm.models import TokenUsage, ToolCall
from va_llm.wire_fields import Invalid, read_count, read_object, read_text, read_usage

__all__ = ["ChunkDeltas", "StreamAssembler"]

_DATA: Final = "data:"
_DONE: Final = "[DONE]"


@dataclass(frozen=True, slots=True)
class ChunkDeltas:
    """What one chunk added: its content text (possibly empty) and whether it carried a token (content or a
    tool-call delta)."""

    content: str
    token: bool


_NOTHING: Final = ChunkDeltas("", token=False)


@dataclass(slots=True)
class _PartialCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


def _choice_delta(chunk: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """The first choice's delta (empty when the chunk has no choices, e.g. the usage chunk) and its
    ``finish_reason``."""
    choices = chunk.get("choices", [])
    if not isinstance(choices, list):
        msg = "chunk.choices is not an array"
        raise Invalid(msg)
    if not choices:
        return {}, None
    choice = read_object(choices[0], "choices[0]")
    delta = choice.get("delta")
    return (
        {} if delta is None else read_object(delta, "choices[0].delta"),
        read_text(choice.get("finish_reason"), "choices[0].finish_reason"),
    )


def _parse_chunk(data: str) -> dict[str, Any]:
    try:
        document = orjson.loads(data)
    except orjson.JSONDecodeError:
        msg = "a stream chunk is not JSON"
        raise Invalid(msg) from None
    chunk = read_object(document, "the chunk")
    if "error" in chunk:
        error = chunk["error"]
        detail = error.get("message") if isinstance(error, dict) else error
        msg = f"the stream reported an error: {detail}"
        raise Invalid(msg)
    return chunk


class StreamAssembler:
    """Feeds SSE lines of one streaming completion to ``provider_id``/``model``."""

    def __init__(self, *, provider_id: str, model: str) -> None:
        self._provider_id = provider_id
        self._model = model
        self._content: list[str] = []
        self._calls: dict[int, _PartialCall] = {}
        self.finish_reason: str | None = None
        self.usage: TokenUsage | None = None
        self.done = False

    @property
    def text(self) -> str:
        """The content received so far."""
        return "".join(self._content)

    def feed(self, line: str) -> ChunkDeltas:
        """The deltas of one SSE line (nothing for non-data lines and ``[DONE]``)."""
        try:
            return self._feed(line)
        except Invalid as exc:
            message = f"{self._provider_id} {self._model}: {exc}"
        raise LlmProtocolError(message, provider_id=self._provider_id, model=self._model)

    def tool_calls(self, *, complete: bool) -> list[ToolCall]:
        """The tool calls ordered by index; ``complete`` (a finished stream) requires every call to have a
        name, otherwise calls without a name are left out (a cut-off stream)."""
        calls = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if not partial.name:
                if complete:
                    message = f"{self._provider_id} {self._model}: tool_calls[{index}] has no function name"
                    raise LlmProtocolError(message, provider_id=self._provider_id, model=self._model)
                continue
            calls.append(
                ToolCall(id=partial.id, name=partial.name, arguments_json="".join(partial.arguments))
            )
        return calls

    def _feed(self, line: str) -> ChunkDeltas:
        if not line.startswith(_DATA):
            return _NOTHING
        data = line.removeprefix(_DATA).strip()
        if data == _DONE:
            self.done = True
            return _NOTHING
        chunk = _parse_chunk(data)
        usage = read_usage(chunk.get("usage"))
        if usage is not None:
            self.usage = usage
        delta, finish_reason = _choice_delta(chunk)
        if finish_reason is not None:
            self.finish_reason = finish_reason
        content = read_text(delta.get("content"), "delta.content") or ""
        if content:
            self._content.append(content)
        called = self._feed_calls(delta.get("tool_calls"))
        return ChunkDeltas(content, token=bool(content) or called)

    def _feed_calls(self, value: object) -> bool:
        if value is None:
            return False
        if not isinstance(value, list):
            msg = "delta.tool_calls is not an array"
            raise Invalid(msg)
        for position, item in enumerate(value):
            where = f"delta.tool_calls[{position}]"
            call = read_object(item, where)
            partial = self._calls.setdefault(read_count(call.get("index"), f"{where}.index"), _PartialCall())
            function = call.get("function")
            function = {} if function is None else read_object(function, f"{where}.function")
            partial.id = partial.id or read_text(call.get("id"), f"{where}.id") or ""
            partial.name = partial.name or read_text(function.get("name"), f"{where}.function.name") or ""
            arguments = read_text(function.get("arguments"), f"{where}.function.arguments")
            if arguments:
                partial.arguments.append(arguments)
        return bool(value)
