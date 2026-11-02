"""Transcript downloads of ``GET /v1/calls/{call_id}/transcript`` (specs/05-calls-api.md §7.6; Decision
P05-T16b).

``text`` is the Call Object's ``concatenated_transcript``. SRT and WebVTT carry one cue per spoken turn with a
``User:``/``Agent:`` prefix; turns whose text is blank are skipped, line breaks inside a turn collapse to one
space, negative times clamp to 0, and an end before the start becomes the start. WebVTT escapes ``&``, ``<``,
and ``>`` (which also neutralises ``-->`` inside the text).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, Literal

from va_domain.call_object.mapping import concatenated_transcript
from va_domain.call_object.models import Transcript, TranscriptRole

__all__ = ["speaker_label", "to_srt", "to_vtt", "transcript_text"]

_MS_PER_SECOND: Final = 1000
_SECONDS_PER_MINUTE: Final = 60
_SECONDS_PER_HOUR: Final = 3600
_LINE_BREAK_RUN: Final = re.compile(r"\s*[\r\n]\s*")
_VTT_ESCAPES: Final = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
_VTT_HEADER: Final = "WEBVTT\n"


def speaker_label(role: TranscriptRole) -> Literal["User", "Agent"]:
    """``User`` for the caller; every other speaker (assistant, agent action, human agent) is ``Agent``."""
    return "User" if role == "user" else "Agent"


def transcript_text(turns: Sequence[Transcript]) -> str:
    """The ``text`` download: the Call Object's ``concatenated_transcript``."""
    return concatenated_transcript(turns)


def _cue_text(turn: Transcript) -> str:
    return f"{speaker_label(turn.user)}: {_LINE_BREAK_RUN.sub(' ', turn.text.strip())}"


def _bounds(turn: Transcript) -> tuple[int, int]:
    start = max(turn.start_ms, 0)
    return start, max(turn.end_ms, start)


def _timestamp(ms: int, separator: str) -> str:
    seconds, millis = divmod(ms, _MS_PER_SECOND)
    hours, rest = divmod(seconds, _SECONDS_PER_HOUR)
    minutes, seconds = divmod(rest, _SECONDS_PER_MINUTE)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _spoken(turns: Sequence[Transcript]) -> list[Transcript]:
    return [turn for turn in turns if turn.text.strip()]


def to_srt(turns: Sequence[Transcript]) -> str:
    """SubRip: numbered cues ``HH:MM:SS,mmm --> HH:MM:SS,mmm``; ``""`` without spoken turns."""
    cues = []
    for number, turn in enumerate(_spoken(turns), start=1):
        start, end = _bounds(turn)
        cues.append(f"{number}\n{_timestamp(start, ',')} --> {_timestamp(end, ',')}\n{_cue_text(turn)}\n")
    return "\n".join(cues)


def _vtt_escape(text: str) -> str:
    for raw, escaped in _VTT_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def to_vtt(turns: Sequence[Transcript]) -> str:
    """WebVTT: ``WEBVTT`` header, unnumbered cues ``HH:MM:SS.mmm``; only the header without spoken turns."""
    cues = []
    for turn in _spoken(turns):
        start, end = _bounds(turn)
        cues.append(f"{_timestamp(start, '.')} --> {_timestamp(end, '.')}\n{_vtt_escape(_cue_text(turn))}\n")
    return "\n".join([_VTT_HEADER, *cues])
