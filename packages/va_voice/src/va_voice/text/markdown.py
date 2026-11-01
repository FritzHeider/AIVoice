"""Markdown and whitespace normalization of LLM text (specs/09-tts-elevenlabs.md §5 step 1).

Rules (specs/09 §5 Decision P04-T06), applied per line (lines end at ``\\n`` or ``\\r``):

1. Line start: after optional whitespace, every heading run of ``#`` followed by whitespace or the end of
   the line is removed (repeatedly), then at most one list marker (``-``, ``*``, ``+`` or 1-3 ASCII digits
   followed by ``.``/``)``) that is followed by whitespace.
2. In the rest of the line every backtick and every ``*`` is removed; a run of ``_`` is removed unless the
   characters on both sides of it are letters or digits (``user_id`` stays, ``_word_`` becomes ``word``).
3. Line breaks become spaces, whitespace runs collapse to one space, and the result is stripped.

:func:`normalize_text` is the reference; :class:`MarkdownStripper` produces exactly the same text from any
split of the input into deltas.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["MarkdownStripper", "normalize_text"]

_LINE_BREAK: Final = re.compile(r"\r\n|\r|\n")
_PREFIX: Final = re.compile(r"(?:\s*#+(?=\s|\Z))*(?:\s*(?:[-*+]|[0-9]{1,3}[.)])(?=\s))?")
# A line start that more characters could still turn into a (longer) markup prefix.
_UNDECIDED: Final = re.compile(r"(?:\s*#+(?=\s))*\s*(?:#+|[-*+]|[0-9]{1,3}[.)]?)?")
_DROPPED: Final = frozenset("`*")
_UNDERSCORES: Final = re.compile(r"_+")


def _strip_body(body: str) -> str:
    text = body.replace("`", "").replace("*", "")

    def keep(match: re.Match[str]) -> str:
        start, end = match.span()
        inner = start > 0 and end < len(text) and text[start - 1].isalnum() and text[end].isalnum()
        return match.group() if inner else ""

    return _UNDERSCORES.sub(keep, text)


def normalize_text(raw: str) -> str:
    """``raw`` with markdown artefacts removed and whitespace collapsed (the specs/09 §5 normal form)."""
    lines = (_strip_body(line[_line_prefix_end(line) :]) for line in _LINE_BREAK.split(raw))
    return " ".join(" ".join(lines).split())


def _line_prefix_end(line: str) -> int:
    match = _PREFIX.match(line)
    return match.end() if match is not None else 0


class MarkdownStripper:
    """Streaming twin of :func:`normalize_text`.

    For any split of a text into deltas, ``"".join(feed(d) for d in deltas) + finish()`` equals
    ``normalize_text(text)``. Text is held back only while its normal form can still change: an undecided
    line start, a run of ``_`` waiting for its right neighbour, and trailing whitespace (see
    :attr:`space_pending`).
    """

    def __init__(self) -> None:
        self._out: list[str] = []
        self._started = False
        self._pending_space = False
        self._reset_line()

    @property
    def space_pending(self) -> bool:
        """True when whitespace follows the text released so far (it is emitted before the next character)."""
        return self._pending_space and self._started

    def feed(self, raw: str) -> str:
        """Consume ``raw`` and return the normalized text that is now final."""
        for char in raw:
            self._char(char)
        return self._take()

    def finish(self) -> str:
        """Return the rest of the normalized text (trailing whitespace dropped) and reset for reuse."""
        self._end_line()
        rest = self._take()
        self._started = False
        self._pending_space = False
        return rest

    def _reset_line(self) -> None:
        self._line = ""
        self._in_body = False
        self._prev = ""
        self._underscores = 0

    def _take(self) -> str:
        text = "".join(self._out)
        self._out.clear()
        return text

    def _char(self, char: str) -> None:
        if char in "\r\n":
            self._end_line()
        elif self._in_body:
            self._body_char(char)
        else:
            self._line += char
            if _UNDECIDED.fullmatch(self._line) is None:
                self._open_body()

    def _open_body(self) -> None:
        line = self._line
        self._line = ""
        self._in_body = True
        for char in line[_line_prefix_end(line) :]:
            self._body_char(char)

    def _end_line(self) -> None:
        if not self._in_body:
            self._open_body()
        self._reset_line()
        self._emit(" ")

    def _body_char(self, char: str) -> None:
        if char in _DROPPED:
            return
        if char == "_":
            self._underscores += 1
            return
        if self._underscores:
            if self._prev.isalnum() and char.isalnum():
                self._emit("_" * self._underscores)
            self._underscores = 0
        self._emit(char)
        self._prev = char

    def _emit(self, text: str) -> None:
        if text.isspace():
            self._pending_space = True
            return
        if self._pending_space and self._started:
            self._out.append(" ")
        self._out.append(text)
        self._started = True
        self._pending_space = False
