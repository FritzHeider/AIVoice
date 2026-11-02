"""cl100k token counting from the vendored rank file (specs/10 §6 Decision P05-T09b)."""

from __future__ import annotations

import gzip
import hashlib
import inspect
from importlib import resources

import pytest
from tiktoken_ext import openai_public

from va_llm import tokens
from va_llm.models import ChatMessage, ToolCall
from va_llm.tokens import (
    CL100K_SHA256,
    MESSAGE_OVERHEAD_TOKENS,
    TokenCounter,
    TokenizerDataError,
    default_counter,
    load_cl100k,
)


def test_vendored_file_matches_the_pinned_hash() -> None:
    assert hashlib.sha256(tokens._read_data()).hexdigest() == CL100K_SHA256


def test_vendored_file_is_deterministic_gzip() -> None:
    packed = resources.files("va_llm").joinpath("data", "cl100k_base.tiktoken.gz").read_bytes()
    assert packed[4:8] == b"\x00\x00\x00\x00"  # mtime 0
    assert gzip.decompress(packed) == tokens._read_data()


@pytest.mark.parametrize(
    ("text", "count"),
    [("", 0), ("hello world", 2), ("tiktoken is great!", 6), ("<|endoftext|>", 7)],
)
def test_known_counts(text: str, count: int) -> None:
    assert default_counter().text(text) == count


def test_encoding_round_trips() -> None:
    encoding = load_cl100k()
    text = "Grüße aus Köln — 24 dollars and 50 cents."
    assert encoding.name == "cl100k_base"
    assert encoding.decode(encoding.encode_ordinary(text)) == text
    assert encoding.n_vocab == 100277


def test_message_counts_content_plus_overhead() -> None:
    counter = default_counter()
    message = ChatMessage(role="user", content="hello world")
    assert counter.message(message) == MESSAGE_OVERHEAD_TOKENS + 2
    assert counter.message(ChatMessage(role="assistant")) == MESSAGE_OVERHEAD_TOKENS


def test_message_counts_tool_calls_and_tool_call_id() -> None:
    counter = default_counter()
    call = ToolCall(id="c1", name="end_call", arguments_json='{"reason":"done"}')
    assistant = ChatMessage(role="assistant", tool_calls=(call,))
    tool = ChatMessage(role="tool", content='{"ok":true}', tool_call_id="c1")

    assert counter.message(assistant) == (
        MESSAGE_OVERHEAD_TOKENS + counter.text("end_call") + counter.text('{"reason":"done"}')
    )
    assert counter.message(tool) == MESSAGE_OVERHEAD_TOKENS + counter.text('{"ok":true}') + counter.text("c1")
    assert counter.messages([assistant, tool]) == counter.message(assistant) + counter.message(tool)


def test_message_memo_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tokens, "MEMO_LIMIT", 2)
    counter = TokenCounter(load_cl100k())
    first = ChatMessage(role="user", content="one")
    counter.message(first)
    counter.message(ChatMessage(role="user", content="two"))
    assert counter.message(first) == MESSAGE_OVERHEAD_TOKENS + 1  # memo hit
    counter.message(ChatMessage(role="user", content="three"))
    assert len(counter._memo) == 1


def test_truncate_keeps_a_token_prefix() -> None:
    counter = default_counter()
    text = "The quick brown fox jumps over the lazy dog. " * 10

    cut = counter.truncate(text, 7)

    assert text.startswith(cut)
    assert counter.text(cut) == 7
    assert counter.truncate("short", 7) == "short"
    assert counter.truncate(text, 0) == ""


def test_hash_mismatch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    load_cl100k.cache_clear()
    monkeypatch.setattr(tokens, "_read_data", lambda: b"bWFsaWNpb3Vz 0\n")
    try:
        with pytest.raises(TokenizerDataError, match="sha256"):
            load_cl100k()
    finally:
        load_cl100k.cache_clear()


def test_encoding_parameters_match_tiktokens_definition() -> None:
    source = inspect.getsource(openai_public.cl100k_base)
    assert f'r"""{tokens._PATTERN}"""' in source
    assert f'expected_hash="{CL100K_SHA256}"' in source
    assert load_cl100k()._special_tokens == {
        openai_public.ENDOFTEXT: 100257,
        openai_public.FIM_PREFIX: 100258,
        openai_public.FIM_MIDDLE: 100259,
        openai_public.FIM_SUFFIX: 100260,
        openai_public.ENDOFPROMPT: 100276,
    }


def test_rank_parsing_skips_blank_lines() -> None:
    assert tokens._parse_ranks(b"IQ== 0\n\nIg== 1\n") == {b"!": 0, b'"': 1}
