"""LLM token stream → TTS clauses (specs/09-tts-elevenlabs.md §5, rules in the §5 Decision P04-T06).

Each delta is normalized by :class:`~va_voice.text.markdown.MarkdownStripper` and appended to a buffer; the
earliest valid sentence boundary, clause boundary, or length cap cuts a clause off the buffer. Every clause is
returned stripped with exactly one trailing space, ready for ``TtsSocket.send(ctx, clause, flush=True)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Final

from va_voice.text.markdown import MarkdownStripper

__all__ = [
    "ABBREVIATIONS",
    "CLAUSE_END",
    "CLAUSE_MIN_WORDS",
    "FIRST_CLAUSE_MIN_WORDS",
    "FIRST_SENTENCE_MIN_WORDS",
    "LENGTH_CAP",
    "SENTENCE_END",
    "SENTENCE_MIN_WORDS",
    "ClauseChunker",
    "chunk_stream",
]

ABBREVIATIONS: Final = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "st",
        "jr",
        "sr",
        "inc",
        "ltd",
        "co",
        "corp",
        "vs",
        "etc",
        "e.g",
        "i.e",
        "a.m",
        "p.m",
        "u.s",
        "no",
        "ave",
        "blvd",
        "dept",
        "approx",
        "est",
        "mt",
        "ft",
        "apt",
    }
)
SENTENCE_END: Final = ".!?"
CLAUSE_END: Final = ",;:"
EM_DASH: Final = "\u2014"
FIRST_SENTENCE_MIN_WORDS: Final = 2
SENTENCE_MIN_WORDS: Final = 3
FIRST_CLAUSE_MIN_WORDS: Final = 4
CLAUSE_MIN_WORDS: Final = 7
LENGTH_CAP: Final = 160

_OPENERS: Final = "\"'([{\u201c\u2018\u00ab"
# "no" is an abbreviation only before a number ("No. 5"); "No. I can't" is a sentence.
_NUMBER_ABBREVIATIONS: Final = frozenset({"no"})


def _word_count(text: str) -> int:
    return sum(1 for token in text.split() if any(char.isalnum() for char in token))


def _has_content(text: str) -> bool:
    return any(char.isalnum() for char in text)


class ClauseChunker:
    """Stateful clause splitter for one utterance at a time; :meth:`flush` ends the utterance and resets."""

    def __init__(self) -> None:
        self._stripper = MarkdownStripper()
        self._buf = ""
        self._emitted_count = 0

    @property
    def emitted_count(self) -> int:
        """Clauses emitted in the current utterance."""
        return self._emitted_count

    @property
    def pending(self) -> str:
        """Normalized text not yet emitted."""
        return self._buf

    def feed(self, delta: str) -> list[str]:
        """Consume one LLM text delta; return the clauses that are now complete."""
        self._buf += self._stripper.feed(delta)
        return self._drain(final=False)

    def flush(self) -> list[str]:
        """End of stream: return the remaining clauses (punctuation-only rest dropped) and reset."""
        self._buf += self._stripper.finish()
        clauses = self._drain(final=True)
        rest = self._buf.strip()
        if _has_content(rest):
            clauses.append(f"{rest} ")
        self._buf = ""
        self._emitted_count = 0
        return clauses

    def _drain(self, *, final: bool) -> list[str]:
        clauses: list[str] = []
        while True:
            self._buf = self._buf.lstrip()
            end = self._boundary(at_end=final)
            if end is None:
                return clauses
            clause = self._buf[:end].strip()
            self._buf = self._buf[end:]
            if _has_content(clause):
                clauses.append(f"{clause} ")
                self._emitted_count += 1

    def _boundary(self, *, at_end: bool) -> int | None:
        """Exclusive end of the earliest valid clause in the buffer, or None (also while undecided)."""
        cap = _cap(self._buf)
        space_after_buffer = at_end or self._stripper.space_pending
        for index in range(len(self._buf) if cap is None else cap):
            verdict = self._candidate(index, space_after_buffer=space_after_buffer, at_end=at_end)
            if verdict is None:
                return None
            if verdict:
                return index + 1
        return cap

    def _candidate(self, index: int, *, space_after_buffer: bool, at_end: bool) -> bool | None:
        """Whether ``buf[index]`` ends a clause; None when the text after it is not known yet."""
        buf = self._buf
        char = buf[index]
        followed = buf[index + 1].isspace() if index + 1 < len(buf) else space_after_buffer
        if not followed:
            return False
        first = self._emitted_count == 0
        if char in SENTENCE_END:
            return self._sentence_ok(index, first=first, at_end=at_end)
        if _is_clause_mark(buf, index):
            minimum = FIRST_CLAUSE_MIN_WORDS if first else CLAUSE_MIN_WORDS
            return _word_count(buf[: index + 1]) >= minimum
        return False

    def _sentence_ok(self, index: int, *, first: bool, at_end: bool) -> bool | None:
        buf = self._buf
        minimum = FIRST_SENTENCE_MIN_WORDS if first else SENTENCE_MIN_WORDS
        if _word_count(buf[: index + 1]) < minimum:
            return False
        if buf[index] != ".":
            return True
        token = _token_before(buf, index)
        if token not in ABBREVIATIONS:
            return True
        if token not in _NUMBER_ABBREVIATIONS:
            return False
        rest = buf[index + 1 :].lstrip()
        if not rest:
            return True if at_end else None
        return not rest[0].isdigit()


def _is_clause_mark(buf: str, index: int) -> bool:
    char = buf[index]
    if char in CLAUSE_END or char == EM_DASH:
        return True
    return char == "-" and index > 0 and buf[index - 1].isspace()


def _token_before(buf: str, index: int) -> str:
    start = buf.rfind(" ", 0, index) + 1
    return buf[start:index].rstrip(SENTENCE_END).lstrip(_OPENERS).lower()


def _cap(buf: str) -> int | None:
    if len(buf) < LENGTH_CAP:
        return None
    space = buf.rfind(" ", 0, LENGTH_CAP)
    return space if space > 0 else LENGTH_CAP


async def chunk_stream(
    deltas: AsyncIterable[str], chunker: ClauseChunker | None = None
) -> AsyncIterator[str]:
    """Yield the clauses of an async stream of LLM text deltas, then the :meth:`ClauseChunker.flush` rest."""
    active = ClauseChunker() if chunker is None else chunker
    async for delta in deltas:
        for clause in active.feed(delta):
            yield clause
    for clause in active.flush():
        yield clause
