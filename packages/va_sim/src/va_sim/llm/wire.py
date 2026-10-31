"""Chat completion wire messages (specs/30 §3.4; Decision P03-T06a rule 7), built from the OpenAI contract.

Pure builders: a streamed reply is a list of :class:`Emission` (one SSE ``data:`` event each); ``paced``
emissions are the ones that carry a token (a content token, a tool-call header, or an argument part) and wait
for the time to first token or the token interval before going out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import orjson
from pydantic import JsonValue

from va_sim.llm.reply import Reply, Usage

__all__ = [
    "DONE",
    "MAX_ARGUMENT_PARTS",
    "Emission",
    "Meta",
    "completion_body",
    "paced_count",
    "split_arguments",
    "sse",
    "stream_emissions",
]

DONE: Final = b"data: [DONE]\n\n"
MAX_ARGUMENT_PARTS: Final = 3
_FINGERPRINT: Final = "fp_sim"


@dataclass(frozen=True, slots=True)
class Meta:
    """Fields every chunk of one response shares."""

    id: str
    created: int
    model: str


@dataclass(frozen=True, slots=True)
class Emission:
    chunk: dict[str, JsonValue]
    paced: bool


def sse(chunk: dict[str, JsonValue]) -> bytes:
    return b"data: " + orjson.dumps(chunk) + b"\n\n"


def split_arguments(arguments: str) -> list[str]:
    """``arguments`` in at most three contiguous parts (one empty part for empty arguments)."""
    if not arguments:
        return [""]
    size = -(-len(arguments) // MAX_ARGUMENT_PARTS)
    return [arguments[start : start + size] for start in range(0, len(arguments), size)]


def _chunk(
    meta: Meta, choices: list[JsonValue], *, include_usage: bool, usage: JsonValue = None
) -> dict[str, JsonValue]:
    chunk: dict[str, JsonValue] = {
        "id": meta.id,
        "object": "chat.completion.chunk",
        "created": meta.created,
        "model": meta.model,
        "system_fingerprint": _FINGERPRINT,
        "choices": choices,
    }
    if include_usage:
        chunk["usage"] = usage
    return chunk


def _choice(delta: dict[str, JsonValue], finish_reason: str | None = None) -> list[JsonValue]:
    return [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}]


def stream_emissions(meta: Meta, reply: Reply, usage: Usage, *, include_usage: bool) -> list[Emission]:
    """Role chunk, token chunks, tool-call chunks, the finish chunk, and (``include_usage``) the usage chunk;
    ``[DONE]`` (:data:`DONE`) follows them."""
    emissions = [
        Emission(
            _chunk(meta, _choice({"role": "assistant", "content": ""}), include_usage=include_usage),
            paced=False,
        )
    ]
    emissions.extend(
        Emission(_chunk(meta, _choice({"content": token}), include_usage=include_usage), paced=True)
        for token in reply.tokens
    )
    for index, call in enumerate(reply.tool_calls):
        header: dict[str, JsonValue] = {
            "index": index,
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": ""},
        }
        emissions.append(
            Emission(_chunk(meta, _choice({"tool_calls": [header]}), include_usage=include_usage), paced=True)
        )
        for part in split_arguments(call.arguments):
            delta: dict[str, JsonValue] = {"tool_calls": [{"index": index, "function": {"arguments": part}}]}
            emissions.append(Emission(_chunk(meta, _choice(delta), include_usage=include_usage), paced=True))
    emissions.append(
        Emission(_chunk(meta, _choice({}, reply.finish_reason), include_usage=include_usage), paced=False)
    )
    if include_usage:
        emissions.append(Emission(_chunk(meta, [], include_usage=True, usage=usage.to_json()), paced=False))
    return emissions


def paced_count(reply: Reply) -> int:
    """How many paced emissions ``reply`` streams (the non-streamed answer waits as long)."""
    return len(reply.tokens) + sum(1 + len(split_arguments(call.arguments)) for call in reply.tool_calls)


def completion_body(meta: Meta, reply: Reply, usage: Usage) -> dict[str, JsonValue]:
    """The non-streamed ``chat.completion`` object."""
    message: dict[str, JsonValue] = {"role": "assistant", "content": reply.content, "refusal": None}
    if reply.tool_calls:
        message["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
            for call in reply.tool_calls
        ]
    return {
        "id": meta.id,
        "object": "chat.completion",
        "created": meta.created,
        "model": meta.model,
        "system_fingerprint": _FINGERPRINT,
        "choices": [{"index": 0, "message": message, "logprobs": None, "finish_reason": reply.finish_reason}],
        "usage": usage.to_json(),
    }
