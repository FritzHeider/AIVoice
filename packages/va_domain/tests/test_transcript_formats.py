"""Transcript downloads: text, SRT, and WebVTT (specs/05-calls-api.md §7.6; Decision P05-T16b).

The SRT/VTT outputs are compared byte for byte with golden files in ``fixtures/transcripts``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final, get_args

import pytest

from va_domain.call_object import (
    Transcript,
    TranscriptRole,
    concatenated_transcript,
    speaker_label,
    to_srt,
    to_vtt,
    transcript_text,
)

FIXTURES: Final = Path(__file__).parent / "fixtures" / "transcripts"
AT: Final = datetime(2026, 10, 20, 12, tzinfo=UTC)


def turn(index: int, role: TranscriptRole, text: str, start_ms: int, end_ms: int) -> Transcript:
    return Transcript(
        id=index, user=role, text=text, created_at=AT, start_ms=start_ms, end_ms=end_ms, interrupted=False
    )


BASIC: Final = [
    turn(1, "user", "Hello?", 0, 820),
    turn(2, "assistant", "Hi, this is Ava from Acme Dental.", 1_100, 3_450),
    turn(3, "agent-action", "Transferring you now.", 3_900, 5_010),
    turn(4, "user", "Thanks, bye.", 61_250, 62_000),
]
EDGE: Final = [
    turn(1, "user", "   ", 0, 500),
    turn(2, "assistant", "First line.\n\n  Second line.\r\nThird.", 1_000, 2_000),
    turn(3, "user", "Backwards", 5_000, 4_000),
    turn(4, "human_agent", "Late <b> & --> done", 3_723_004, 3_725_999),
    turn(5, "user", "Negative", -40, -10),
]


def golden(name: str) -> str:
    with (FIXTURES / name).open(encoding="utf-8", newline="") as handle:
        return handle.read()


@pytest.mark.parametrize(
    ("turns", "name"),
    [(BASIC, "basic.srt"), (EDGE, "edge.srt"), (BASIC, "basic.vtt"), (EDGE, "edge.vtt")],
)
def test_outputs_match_the_golden_files(turns: list[Transcript], name: str) -> None:
    render = to_srt if name.endswith(".srt") else to_vtt
    assert render(turns) == golden(name)


@pytest.mark.parametrize("role", get_args(TranscriptRole.__value__))
def test_every_speaker_has_a_label(role: TranscriptRole) -> None:
    assert speaker_label(role) == ("User" if role == "user" else "Agent")


def test_empty_transcripts() -> None:
    assert to_srt([]) == ""
    assert to_vtt([]) == "WEBVTT\n"
    assert transcript_text([]) == ""


def test_only_blank_turns_render_like_an_empty_transcript() -> None:
    blank = [turn(1, "user", " \n ", 0, 10)]
    assert (to_srt(blank), to_vtt(blank)) == ("", "WEBVTT\n")


@pytest.mark.parametrize("turns", [BASIC, EDGE])
def test_text_is_the_concatenated_transcript(turns: list[Transcript]) -> None:
    assert transcript_text(turns) == concatenated_transcript(turns)


def test_srt_cues_are_numbered_only_for_spoken_turns() -> None:
    numbers = [block.split("\n", 1)[0] for block in to_srt(EDGE).split("\n\n")]
    assert numbers == ["1", "2", "3", "4"]


def test_vtt_never_contains_a_raw_arrow_inside_cue_text() -> None:
    cue_lines = [line for line in to_vtt(EDGE).splitlines() if line.startswith(("User:", "Agent:"))]
    assert all("-->" not in line and "<" not in line for line in cue_lines)
