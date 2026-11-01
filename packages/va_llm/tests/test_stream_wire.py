"""SSE chunk parsing and assembly (Decision P03-T13b rule 3)."""

from __future__ import annotations

import orjson
import pytest
from llm_client_helpers import chunk

from va_llm import LlmProtocolError, TokenUsage, ToolCall
from va_llm.stream_wire import ChunkDeltas, StreamAssembler


def _line(document: object) -> str:
    return "data: " + orjson.dumps(document).decode()


def _assembler() -> StreamAssembler:
    return StreamAssembler(provider_id="p", model="m")


def test_content_deltas_accumulate_until_done() -> None:
    assembler = _assembler()
    assert assembler.feed(_line(chunk({"role": "assistant", "content": ""}))) == ChunkDeltas("", token=False)
    assert assembler.feed(_line(chunk({"content": "Hel"}))) == ChunkDeltas("Hel", token=True)
    assert assembler.feed("data:" + orjson.dumps(chunk({"content": "lo"})).decode()) == ChunkDeltas(
        "lo", token=True
    )
    assert assembler.feed(_line(chunk(finish_reason="stop"))) == ChunkDeltas("", token=False)
    assert not assembler.done
    assert assembler.feed("data: [DONE]") == ChunkDeltas("", token=False)
    assert (assembler.text, assembler.finish_reason, assembler.done) == ("Hello", "stop", True)


@pytest.mark.parametrize("line", ["", ": ping", "event: message", "id: 4", "retry: 100"])
def test_non_data_lines_are_ignored(line: str) -> None:
    assembler = _assembler()
    assert assembler.feed(line) == ChunkDeltas("", token=False)
    assert not assembler.done


@pytest.mark.parametrize("key", ["reasoning_content", "reasoning"])
def test_reasoning_deltas_are_not_tokens(key: str) -> None:
    assembler = _assembler()
    assert assembler.feed(_line(chunk({key: "thinking...", "content": None}))) == ChunkDeltas("", token=False)
    assert assembler.text == ""


def test_usage_comes_from_the_trailing_chunk_and_finish_reason_keeps_the_last() -> None:
    assembler = _assembler()
    assembler.feed(_line(chunk(finish_reason="length")))
    assembler.feed(_line(chunk({"content": None}, finish_reason=None)))
    assembler.feed(_line(chunk(usage=(40, 9), choices=False)))
    assert assembler.usage == TokenUsage(prompt_tokens=40, completion_tokens=9)
    assert assembler.finish_reason == "length"


def test_a_chunk_without_choices_or_delta_is_nothing() -> None:
    assembler = _assembler()
    assert assembler.feed(_line({"id": "x"})) == ChunkDeltas("", token=False)
    assert assembler.feed(_line({"choices": [{"index": 0}]})) == ChunkDeltas("", token=False)


def test_tool_calls_assemble_by_index_and_the_header_is_a_token() -> None:
    assembler = _assembler()
    header = {"index": 1, "id": "b", "type": "function", "function": {"name": "two", "arguments": ""}}
    assert assembler.feed(_line(chunk({"tool_calls": [header]}))).token
    assert assembler.feed(
        _line(chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "one"}}]}))
    ).token
    assembler.feed(_line(chunk({"tool_calls": [{"index": 1, "function": {"arguments": '{"x":'}}]})))
    assembler.feed(_line(chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]})))
    assembler.feed(
        _line(chunk({"tool_calls": [{"index": 1, "id": "ignored", "function": {"arguments": "1}"}}]}))
    )
    assert assembler.tool_calls(complete=True) == [
        ToolCall(id="a", name="one", arguments_json="{}"),
        ToolCall(id="b", name="two", arguments_json='{"x":1}'),
    ]


def test_an_empty_tool_call_list_is_not_a_token() -> None:
    assert _assembler().feed(_line(chunk({"tool_calls": []}))) == ChunkDeltas("", token=False)


def test_a_nameless_call_fails_a_finished_stream_and_is_dropped_from_a_cut_one() -> None:
    assembler = _assembler()
    assembler.feed(_line(chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "one"}}]})))
    assembler.feed(_line(chunk({"tool_calls": [{"index": 1, "id": "b"}]})))
    with pytest.raises(LlmProtocolError, match=r"p m: tool_calls\[1\] has no function name"):
        assembler.tool_calls(complete=True)
    assert assembler.tool_calls(complete=False) == [ToolCall(id="a", name="one", arguments_json="")]


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("data: {oops", "a stream chunk is not JSON"),
        ("data: [1]", "the chunk is not an object"),
        (_line({"error": {"message": "overloaded"}}), "the stream reported an error: overloaded"),
        (_line({"error": "boom"}), "the stream reported an error: boom"),
        (_line({"choices": {"a": 1}}), "chunk.choices is not an array"),
        (_line({"choices": [1]}), r"choices\[0\] is not an object"),
        (_line({"choices": [{"delta": "x"}]}), r"choices\[0\].delta is not an object"),
        (_line({"choices": [{"delta": {"content": 5}}]}), "delta.content is not a string"),
        (_line({"choices": [{"delta": {}, "finish_reason": 1}]}), "finish_reason is not a string"),
        (_line({"choices": [{"delta": {"tool_calls": {}}}]}), "delta.tool_calls is not an array"),
        (
            _line({"choices": [{"delta": {"tool_calls": [{"id": "x"}]}}]}),
            "index is not a non-negative integer",
        ),
        (
            _line({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": 3}]}}]}),
            "function is not an object",
        ),
        (_line({"usage": {"prompt_tokens": -1, "completion_tokens": 0}}), "usage.prompt_tokens"),
    ],
)
def test_malformed_chunks_are_protocol_errors(line: str, message: str) -> None:
    with pytest.raises(LlmProtocolError, match=message) as caught:
        _assembler().feed(line)
    assert (caught.value.provider_id, caught.value.model, caught.value.error_kind) == ("p", "m", "parse")
