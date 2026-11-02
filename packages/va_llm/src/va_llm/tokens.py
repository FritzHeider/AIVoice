"""Token counting for the conversation window (specs/10-llm-gateway.md §6; Decision 2026-11-02 P05-T09b).

``cl100k_base`` is the approximation used for every model. tiktoken downloads its encodings from the internet
on first use, which pods and tests must never do, so the rank file is vendored as
``data/cl100k_base.tiktoken.gz`` and checked against the same sha256 tiktoken pins. Loading parses ~100 000
ranks (up to about a second of CPU): async callers load it once through ``asyncio.to_thread(default_counter)``
at startup.
"""

from __future__ import annotations

import base64
import functools
import gzip
import hashlib
from collections.abc import Iterable
from importlib import resources

import tiktoken

from va_llm.models import ChatMessage

__all__ = [
    "CL100K_SHA256",
    "MESSAGE_OVERHEAD_TOKENS",
    "TokenCounter",
    "TokenizerDataError",
    "default_counter",
    "load_cl100k",
]

CL100K_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
MESSAGE_OVERHEAD_TOKENS = 4
MEMO_LIMIT = 4096

_DATA_FILE = "cl100k_base.tiktoken.gz"
_PATTERN = (
    r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+"""
    r"""|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
)
_SPECIAL_TOKENS = {
    "<|endoftext|>": 100257,
    "<|fim_prefix|>": 100258,
    "<|fim_middle|>": 100259,
    "<|fim_suffix|>": 100260,
    "<|endofprompt|>": 100276,
}
_HASH_MISMATCH = "the vendored cl100k_base rank file does not match its pinned sha256"


class TokenizerDataError(RuntimeError):
    """The vendored rank file is corrupt or was replaced."""


def _read_data() -> bytes:
    packed = resources.files("va_llm").joinpath("data", _DATA_FILE).read_bytes()
    return gzip.decompress(packed)


def _parse_ranks(data: bytes) -> dict[bytes, int]:
    ranks: dict[bytes, int] = {}
    for line in data.splitlines():
        if line:
            token, rank = line.split()
            ranks[base64.b64decode(token)] = int(rank)
    return ranks


@functools.cache
def load_cl100k() -> tiktoken.Encoding:
    """The ``cl100k_base`` encoding built from the vendored file (never from the network)."""
    data = _read_data()
    if hashlib.sha256(data).hexdigest() != CL100K_SHA256:
        raise TokenizerDataError(_HASH_MISMATCH)
    return tiktoken.Encoding(
        name="cl100k_base",
        pat_str=_PATTERN,
        mergeable_ranks=_parse_ranks(data),
        special_tokens=_SPECIAL_TOKENS,
    )


class TokenCounter:
    """Counts text and chat messages: each message costs its tokens plus :data:`MESSAGE_OVERHEAD_TOKENS`.

    Message counts are memoised (messages are frozen, so they hash by value); the memo is cleared when it
    reaches :data:`MEMO_LIMIT` entries.
    """

    def __init__(self, encoding: tiktoken.Encoding) -> None:
        self._encoding = encoding
        self._memo: dict[ChatMessage, int] = {}

    def text(self, text: str) -> int:
        """Tokens of ``text``; special-token strings count as ordinary text."""
        return len(self._encoding.encode_ordinary(text)) if text else 0

    def message(self, message: ChatMessage) -> int:
        """Tokens of one message: content, tool calls (name + arguments), tool call id, and the overhead."""
        cached = self._memo.get(message)
        if cached is not None:
            return cached
        count = MESSAGE_OVERHEAD_TOKENS + self.text(message.content or "")
        for call in message.tool_calls:
            count += self.text(call.name) + self.text(call.arguments_json)
        count += self.text(message.tool_call_id or "")
        if len(self._memo) >= MEMO_LIMIT:
            self._memo.clear()
        self._memo[message] = count
        return count

    def messages(self, messages: Iterable[ChatMessage]) -> int:
        """Sum of :meth:`message` over ``messages``."""
        return sum(self.message(message) for message in messages)

    def truncate(self, text: str, max_tokens: int) -> str:
        """The text of the first ``max_tokens`` tokens of ``text`` (``text`` itself when it already fits)."""
        tokens = self._encoding.encode_ordinary(text)
        if len(tokens) <= max_tokens:
            return text
        return self._encoding.decode(tokens[: max(max_tokens, 0)])


@functools.cache
def default_counter() -> TokenCounter:
    """The process-wide counter over :func:`load_cl100k`."""
    return TokenCounter(load_cl100k())
