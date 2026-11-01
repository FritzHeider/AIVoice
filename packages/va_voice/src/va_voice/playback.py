"""Playback tracking and barge-in truncation (specs/07-media-pipeline.md §7 step 6 and §9, Decision P04-T08).

:class:`PlaybackTracker` follows one agent utterance on the outbound side: the outbound clock reports every
frame it sends and every segment boundary (it sends the returned mark name to the transport), the transport
reports mark echoes. From those it estimates how much audio the caller has heard (``played_ms``) and whether
the agent is still speaking (``is_playing``). The tracker owns the utterance's :class:`AlignmentTimeline`, so
``played_text(at)`` turns the estimate into the whole words heard, and :func:`truncate_assistant_message`
builds what barge-in stores.
Everything is synchronous and takes explicit ms timestamps from the caller's monotonic clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from va_voice.audio.formats import FRAME_MS, ULAW_8000, AudioFormat
from va_voice.text.alignment import AlignmentTimeline, SpokenPrefix

__all__ = [
    "INTERRUPTED_SUFFIX",
    "LEAD_MS_ESTIMATE",
    "MARK_LOSS_GRACE_MS",
    "PlaybackTracker",
    "Segment",
    "TruncatedMessage",
    "mark_name",
    "parse_mark",
    "truncate_assistant_message",
]

LEAD_MS_ESTIMATE: Final = 60
"""How far the outbound clock runs ahead of what the caller hears (at most 3 frames, specs/07 §9 step 4)."""

MARK_LOSS_GRACE_MS: Final = 200
"""Without the last mark echo, playback ends this long after the estimate reaches the end of the audio."""

INTERRUPTED_SUFFIX: Final = "—"
"""Appended to the spoken text in the conversation history after a barge-in (specs/07 §7 step 6)."""

_MARK = re.compile(r"seg:(\d+):(\d+)")


def mark_name(turn: int, idx: int) -> str:
    """The transport mark sent after segment ``idx`` of utterance ``turn``: ``seg:{turn}:{idx}``."""
    return f"seg:{turn}:{idx}"


def parse_mark(name: str) -> tuple[int, int] | None:
    """``(turn, idx)`` of a mark produced by :func:`mark_name`, or ``None`` for any other name."""
    match = _MARK.fullmatch(name)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True, slots=True)
class Segment:
    """Frames ``[start_ms, end_ms)`` of the utterance (frames sent x 20 ms) carrying ``chars`` characters."""

    idx: int
    start_ms: int
    end_ms: int
    chars: int


class PlaybackTracker:
    """Playback state of one agent utterance (one TTS context, one ``turn`` number in the mark names)."""

    __slots__ = (
        "_confirmed_ms",
        "_echoed",
        "_finished",
        "_frames",
        "_lead_ms",
        "_segments",
        "_stopped_at",
        "_t_first",
        "_timeline",
        "_turn",
    )

    def __init__(self, turn: int, fmt: AudioFormat = ULAW_8000, *, lead_ms: int = LEAD_MS_ESTIMATE) -> None:
        if turn < 0:
            message = f"turn must be >= 0, got {turn}"
            raise ValueError(message)
        self._turn = turn
        self._lead_ms = lead_ms
        self._timeline = AlignmentTimeline(fmt)
        self._t_first: float | None = None
        self._frames = 0
        self._segments: list[Segment] = []
        self._confirmed_ms = 0
        self._echoed = -1
        self._finished = False
        self._stopped_at: float | None = None

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def timeline(self) -> AlignmentTimeline:
        """The utterance's alignment timeline (feed clauses and TTS chunks into it)."""
        return self._timeline

    @property
    def t_first_frame_sent(self) -> float | None:
        return self._t_first

    @property
    def frames_sent(self) -> int:
        return self._frames

    @property
    def total_ms(self) -> int:
        """Audio sent so far: ``frames_sent * 20``."""
        return self._frames * FRAME_MS

    @property
    def segments(self) -> tuple[Segment, ...]:
        return tuple(self._segments)

    @property
    def confirmed_ms(self) -> int:
        """End of the latest segment whose mark came back (a floor for ``played_ms``)."""
        return self._confirmed_ms

    @property
    def finished(self) -> bool:
        """No more audio follows (:meth:`finish` was called)."""
        return self._finished

    @property
    def stopped(self) -> bool:
        """Playback was cut by a barge-in (:meth:`stop`)."""
        return self._stopped_at is not None

    def frame_sent(self, now_ms: float) -> None:
        """One 20 ms frame went to the transport at ``now_ms``; ignored after :meth:`stop`."""
        if self._stopped_at is not None:
            return
        if self._finished:
            message = "frame sent after the utterance finished"
            raise RuntimeError(message)
        if self._t_first is None:
            self._t_first = now_ms
        self._frames += 1

    def end_segment(self, chars: int) -> str:
        """Close the segment sent since the previous boundary and return the mark name to send after it."""
        if self._finished or self._stopped_at is not None:
            message = "segment ended after the utterance finished or stopped"
            raise RuntimeError(message)
        start_ms = self._segments[-1].end_ms if self._segments else 0
        if self.total_ms <= start_ms:
            message = "a segment needs at least one frame"
            raise ValueError(message)
        segment = Segment(len(self._segments), start_ms, self.total_ms, chars)
        self._segments.append(segment)
        return mark_name(self._turn, segment.idx)

    def finish(self) -> None:
        """No more audio for this utterance; every sent frame must belong to a segment."""
        last_end = self._segments[-1].end_ms if self._segments else 0
        if self.total_ms != last_end:
            message = "frames after the last segment: call end_segment before finish"
            raise RuntimeError(message)
        self._finished = True

    def on_mark(self, name: str) -> bool:
        """Apply a mark echo; ``False`` for another utterance, an unknown segment, or after :meth:`stop`."""
        parsed = parse_mark(name)
        if parsed is None or self._stopped_at is not None:
            return False
        turn, idx = parsed
        if turn != self._turn or idx >= len(self._segments):
            return False
        self._confirmed_ms = max(self._confirmed_ms, self._segments[idx].end_ms)
        self._echoed = max(self._echoed, idx)
        return True

    def played_ms(self, now_ms: float) -> float:
        """Audio heard by ``now_ms``: the clamped lead-corrected estimate, never below confirmed marks."""
        if self._stopped_at is not None:
            now_ms = min(now_ms, self._stopped_at)
        return max(float(self._confirmed_ms), self._estimate(now_ms))

    def is_playing(self, now_ms: float) -> bool:
        """True from the first frame until the last mark echo (or total + 200 ms of estimate without it)."""
        if self._t_first is None or self._stopped_at is not None:
            return False
        if not self._finished:
            return True
        if self._segments and self._echoed == len(self._segments) - 1:
            return False
        return (now_ms - self._t_first) - self._lead_ms < self.total_ms + MARK_LOSS_GRACE_MS

    def completed(self, now_ms: float) -> bool:
        """Played to the end: finished, not stopped, and no longer playing."""
        return self._finished and self._stopped_at is None and not self.is_playing(now_ms)

    def stop(self, at_ms: float) -> float:
        """Barge-in at ``at_ms``: freeze playback, return ``played_ms(at_ms)`` (later calls keep it)."""
        if self._stopped_at is None:
            self._stopped_at = at_ms
        return self.played_ms(at_ms)

    def played_text(self, at_ms: float) -> SpokenPrefix:
        """Whole words of the original text heard by ``at_ms``."""
        return self._timeline.text_until(self.played_ms(at_ms))

    def _estimate(self, now_ms: float) -> float:
        if self._t_first is None:
            return 0.0
        elapsed = (now_ms - self._t_first) - self._lead_ms
        return float(min(max(elapsed, 0.0), self.total_ms))


@dataclass(frozen=True, slots=True)
class TruncatedMessage:
    """The assistant turn after a barge-in (specs/07 §7 steps 6-7 and the false-barge-in resume)."""

    text: str
    """Full planned text so far (persisted as ``text``)."""
    text_spoken: str
    """Whole words heard (persisted as ``text_spoken``)."""
    history_text: str
    """What the conversation history keeps: ``text_spoken`` followed by an em dash."""
    played_chars: int
    total_chars: int
    unspoken: str
    """The remainder of ``text`` after ``text_spoken`` (re-spoken after a false barge-in)."""


def truncate_assistant_message(timeline: AlignmentTimeline, played_ms: float) -> TruncatedMessage:
    """Cut the utterance in ``timeline`` at ``played_ms`` on a word boundary of the original text."""
    text = timeline.original_text.strip()
    spoken = timeline.text_until(played_ms).text.lstrip()
    return TruncatedMessage(
        text=text,
        text_spoken=spoken,
        history_text=f"{spoken}{INTERRUPTED_SUFFIX}",
        played_chars=len(spoken),
        total_chars=len(text),
        unspoken=text[len(spoken) :].strip(),
    )
