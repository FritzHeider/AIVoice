"""Frame queues of a FakeTelnyx media stream (specs/30-testing-and-evals.md §3.2, Decision 2026-10-29 P03-T03b
rules 5, 6, and 10).

:class:`Playout` is the virtual playout clock of agent audio: every 20 ms tick plays one frame of queued audio
(silence when empty, a partial last frame padded with silence); a mark is due once all audio queued before it
has played, or at the next tick when nothing was queued; ``clear`` drops queued audio and makes every pending
mark due. :class:`CallerQueue` cuts queued caller audio into frames and reports when each utterance's first
and last bytes go out. Both are pure: the media session drives them from its tick loop.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from va_sim.telnyx.media_format import MediaFormat

__all__ = ["CallerFrame", "CallerQueue", "Playout"]


class Playout:
    """Queued agent audio and marks, positioned by byte offset in the agent stream."""

    def __init__(self, fmt: MediaFormat) -> None:
        self._fmt = fmt
        self._queue = bytearray()
        self._pushed = 0
        self._played = 0
        self._marks: deque[tuple[int, str]] = deque()
        self._due: list[str] = []

    @property
    def queued_ms(self) -> int:
        return len(self._queue) // self._fmt.bytes_per_ms

    def push(self, audio: bytes) -> None:
        self._queue.extend(audio)
        self._pushed += len(audio)

    def mark(self, name: str) -> None:
        """Place a mark after the audio queued so far (due at the next tick when nothing is queued)."""
        if self._queue:
            self._marks.append((self._pushed, name))
        else:
            self._due.append(name)

    def clear(self) -> None:
        """Drop queued audio; every pending mark becomes due, in queue order."""
        self._queue.clear()
        self._played = self._pushed
        self._due.extend(name for _, name in self._marks)
        self._marks.clear()

    def tick(self) -> tuple[bytes, list[str]]:
        """Play one frame: the wire bytes played (padded to a full frame) and the marks now due."""
        size = self._fmt.frame_bytes
        played = bytes(self._queue[:size])
        del self._queue[:size]
        self._played += len(played)
        while self._marks and self._marks[0][0] <= self._played:
            self._due.append(self._marks.popleft()[1])
        due, self._due = self._due, []
        return played + self._fmt.silence(size - len(played)), due


@dataclass(slots=True)
class _Span:
    entry: dict[str, object]
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class CallerFrame:
    """One caller frame and the utterance timeline entries whose first or last byte it carries."""

    payload: bytes
    started: list[dict[str, object]] = field(default_factory=list)
    ended: list[dict[str, object]] = field(default_factory=list)


class CallerQueue:
    """Caller audio waiting to be sent, positioned by byte offset in the caller stream."""

    def __init__(self, fmt: MediaFormat) -> None:
        self._fmt = fmt
        self._queue = bytearray()
        self._sent = 0
        self._spans: deque[_Span] = deque()

    @property
    def sent(self) -> int:
        """Bytes taken into frames so far (the caller stream offset of the next byte)."""
        return self._sent

    def push(self, audio: bytes, entry: dict[str, object] | None = None) -> int:
        """Queue ``audio``; ``entry`` (an utterance timeline entry) is reported when it starts and ends.

        Returns the stream offset just past ``audio``: it has been sent once :attr:`sent` reaches it.
        """
        start = self._sent + len(self._queue)
        self._queue.extend(audio)
        if entry is not None and audio:
            self._spans.append(_Span(entry, start, start + len(audio)))
        return start + len(audio)

    def next_frame(self) -> CallerFrame:
        """The next 20 ms frame (silence-padded when the queue runs short)."""
        size = self._fmt.frame_bytes
        taken = bytes(self._queue[:size])
        del self._queue[:size]
        first, self._sent = self._sent, self._sent + len(taken)
        started = [span.entry for span in self._spans if first <= span.start < self._sent]
        ended: list[dict[str, object]] = []
        while self._spans and self._spans[0].end <= self._sent:
            ended.append(self._spans.popleft().entry)
        return CallerFrame(taken + self._fmt.silence(size - len(taken)), started, ended)
