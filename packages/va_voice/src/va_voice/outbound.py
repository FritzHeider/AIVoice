"""Outbound pacing and background mixing (specs/07-media-pipeline.md §9, §10.2, §15).

:class:`FramePacer` is the pure, ms-based schedule: frames are due at ``t0 + (n - lead) x 20`` from an anchor
``t0`` (targets are computed from the anchor, never by accumulating sleeps, so they do not drift), which keeps
at most ``lead`` frames (3 = 60 ms) queued at Telnyx ahead of playback. Once wall-clock time passes the end of
what was sent (Telnyx has drained) the next frame re-anchors the schedule; ``reset`` after a clear does the
same.

:class:`FrameMixer` turns a 20 ms TTS frame into a wire frame: μ-law speech without background is passed
through untouched; otherwise speech is decoded, the background frame is added at unit gain, clipped to int16,
and encoded to the leg codec (μ-law, or L16 in ``TELNYX_L16_BYTE_ORDER`` for hd_audio). Agent speech is never
ducked and the background never goes to the STT.

:class:`OutboundAudio` runs the loop: it re-frames TTS audio, sends one frame per due slot (an idle frame of
background when no speech is queued, nothing when there is no background), sends a segment's mark right after
the segment's last frame, and reports every frame to the utterance's
:class:`~va_voice.playback.PlaybackTracker`.

A platform **underrun** (specs/07 §9 Decision P05-T17e) is a speech frame sent after Telnyx's buffer drained
while the previous frame sent on the leg was a speech frame of the same utterance (no idle frame, gap wait,
clear, or finish in between): the send loop fell more than the lead behind mid-utterance. TTS starvation is
not an underrun (it sends idle frames or waits in the gap branch, which breaks the continuity). Every underrun
is counted (:attr:`OutboundStats.underruns`, ``on_underrun``) once its frame went out; a clear while that
frame was in flight drops it, uncounted. The ``outbound.underrun`` warning (specs/07 §9 Decisions P08-FIX-3
and P08-FIX-4) is emitted next to the counter, so lines never outnumber counts, and is throttled: the first
underrun of an utterance is logged, then at most one line per :data:`UNDERRUN_LOG_INTERVAL_MS` on the leg,
carrying ``suppressed`` (underruns not logged since the previous line). Its fields are snapshotted before the
send and attribute the stall: ``turn``, ``behind_ms`` (how long Telnyx had been drained),
``since_prev_send_ms``, ``prev_send_await_ms`` (how long the previous ``send_audio`` await took), split by a
:class:`TimedSink` into ``prev_lock_wait_ms`` (waiting behind another send on the socket),
``prev_socket_send_ms`` (the socket write) and ``prev_resume_ms`` (the rest: the event loop resuming the task
after the send completed, i.e. loop lag), ``prev_controls_await_ms`` (marks sent since the previous frame),
``wake_late_ms`` (how far past its due time the loop woke from its pacing sleep: an event-loop stall) and
``queued_frames``.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from va_common.logging import get_logger
from va_common.time import Clock
from va_voice.audio import (
    FRAME_MS,
    PCM16LE_16000,
    ULAW_8000,
    AudioFormat,
    Framer,
    L16ByteOrder,
    gain_for_db,
    pcm16le_to_pcm,
    pcm_to_l16,
    ulaw_decode,
    ulaw_encode,
)
from va_voice.background import BackgroundLoop
from va_voice.playback import PlaybackTracker

__all__ = [
    "LATE_THRESHOLD_MS",
    "LEAD_FRAMES",
    "RATE_LIMIT_PAUSE_MS",
    "RATE_LIMIT_WINDOW_MS",
    "THROTTLED_LEAD_FRAMES",
    "UNDERRUN_LOG_INTERVAL_MS",
    "FrameMixer",
    "FramePacer",
    "OutboundAudio",
    "OutboundSink",
    "OutboundStats",
    "SendTiming",
    "TimedSink",
]

LEAD_FRAMES: Final = 3
"""Frames sent ahead of the playback position (60 ms)."""

THROTTLED_LEAD_FRAMES: Final = 1
"""Lead while backing off after a Telnyx rate-limit error (``100005``)."""

RATE_LIMIT_PAUSE_MS: Final = 100
"""Sends pause this long after a rate-limit error."""

RATE_LIMIT_WINDOW_MS: Final = 5000
"""The reduced lead lasts this long after a rate-limit error."""

LATE_THRESHOLD_MS: Final = 30
"""A frame sent more than this past its target counts as late (specs/07 §21)."""

UNDERRUN_LOG_INTERVAL_MS: Final = 1000.0
"""After an utterance's first logged underrun, at most one ``outbound.underrun`` line per leg this often."""

_INT16_MIN: Final = -32_768
_INT16_MAX: Final = 32_767
_DUE_EPSILON_MS: Final = 0.01
_MS_PER_SECOND: Final = 1000.0

_log = get_logger(__name__)


class FrameMixer:
    """Mixes a background loop under 20 ms TTS frames and encodes them for the wire."""

    __slots__ = ("_background", "_decode", "_encode", "_format", "_gain", "_passthrough")

    def __init__(
        self,
        fmt: AudioFormat,
        *,
        background: BackgroundLoop | None = None,
        gain_db: float = 0.0,
        wire_byte_order: L16ByteOrder = "big",
    ) -> None:
        self._format = fmt
        self._decode, self._encode, self._passthrough = _codec_for(fmt, wire_byte_order)
        if background is not None and background.frame_samples != fmt.frame_samples:
            have, need = background.frame_samples, fmt.frame_samples
            message = f"background frames have {have} samples, {fmt.name} needs {need}"
            raise ValueError(message)
        self._background = background
        self._gain = gain_for_db(gain_db)

    @property
    def format(self) -> AudioFormat:
        """The TTS format the mixer accepts."""
        return self._format

    @property
    def has_background(self) -> bool:
        return self._background is not None

    def speech(self, frame: bytes) -> bytes:
        """The wire frame for one TTS frame (mixed with the next background frame when there is one)."""
        if len(frame) != self._format.frame_bytes:
            message = f"expected a {self._format.frame_bytes}-byte frame, got {len(frame)}"
            raise ValueError(message)
        if self._background is None:
            return frame if self._passthrough else self._encode(self._decode(frame))
        mixed = self._decode(frame).astype(np.int32) + self._background_frame(self._background)
        return self._encode(_clip(mixed))

    def idle(self) -> bytes:
        """A background-only wire frame (only with a background: check :attr:`has_background`)."""
        if self._background is None:
            message = "no background: idle frames are not sent"
            raise RuntimeError(message)
        return self._encode(_clip(self._background_frame(self._background)))

    def _background_frame(self, background: BackgroundLoop) -> NDArray[np.int32]:
        frame = background.next_frame().astype(np.int32)
        if self._gain == 1.0:
            return frame
        return np.rint(frame * self._gain).astype(np.int32)


def _clip(x: NDArray[np.int32]) -> NDArray[np.int16]:
    return np.clip(x, _INT16_MIN, _INT16_MAX).astype(np.int16)


type _Decode = Callable[[bytes], NDArray[np.int16]]
type _Encode = Callable[[NDArray[np.int16]], bytes]


def _codec_for(fmt: AudioFormat, byte_order: L16ByteOrder) -> tuple[_Decode, _Encode, bool]:
    """(decode, encode, passthrough) of a TTS format; ``passthrough``: TTS bytes already are wire bytes."""
    if fmt == ULAW_8000:
        return ulaw_decode, ulaw_encode, True
    if fmt == PCM16LE_16000:

        def encode(x: NDArray[np.int16]) -> bytes:
            return pcm_to_l16(x, byte_order)

        return pcm16le_to_pcm, encode, byte_order == "little"
    message = f"unsupported TTS format {fmt.name}: expected ulaw_8000 or pcm_16000"
    raise ValueError(message)


class FramePacer:
    """The drift-free send schedule with a bounded lead, re-anchored whenever Telnyx's buffer is empty."""

    __slots__ = ("_anchor", "_frame_ms", "_gap", "_lead", "_paused_until", "_sent", "_throttled_until")

    def __init__(self, *, frame_ms: int = FRAME_MS, lead_frames: int = LEAD_FRAMES) -> None:
        if lead_frames < 0:
            message = f"lead_frames must be >= 0, got {lead_frames}"
            raise ValueError(message)
        self._frame_ms = frame_ms
        self._lead = lead_frames
        self._anchor: float | None = None
        self._sent = 0
        self._gap = True
        self._paused_until = -math.inf
        self._throttled_until = -math.inf

    @property
    def anchor_ms(self) -> float | None:
        """Send time of the first frame since the last (re-)anchor."""
        return self._anchor

    @property
    def sent_since_anchor(self) -> int:
        return self._sent

    def lead_frames_at(self, now_ms: float) -> int:
        """The lead in effect: reduced for :data:`RATE_LIMIT_WINDOW_MS` after :meth:`throttle`."""
        return THROTTLED_LEAD_FRAMES if now_ms < self._throttled_until else self._lead

    @property
    def drained_at_ms(self) -> float | None:
        """When Telnyx's buffer runs (or ran) empty: the end of what was sent since the anchor."""
        return None if self._anchor is None else self._anchor + self._sent * self._frame_ms

    def drained(self, now_ms: float) -> bool:
        """Nothing is queued at Telnyx any more (or nothing was ever sent since the last anchor)."""
        return self._anchor is None or now_ms >= self._anchor + self._sent * self._frame_ms

    def next_send_ms(self, now_ms: float) -> float:
        """When the next frame may go out (``now_ms`` or earlier means now)."""
        if self._anchor is None or self.drained(now_ms):
            return max(now_ms, self._paused_until)
        return max(self._nominal(self._anchor, now_ms), self._paused_until)

    def on_sent(self, now_ms: float) -> float:
        """Record a frame sent at ``now_ms``; returns its lateness vs. the target (0 after a gap/reset)."""
        lateness = 0.0
        if self._anchor is not None and not self._gap:
            lateness = max(0.0, now_ms - max(self._nominal(self._anchor, now_ms), self._paused_until))
        if self.drained(now_ms):
            self._anchor = now_ms
            self._sent = 0
        self._sent += 1
        self._gap = False
        return lateness

    def mark_gap(self) -> None:
        """Nothing was available to send: the next frame's wait is not lateness."""
        self._gap = True

    def reset(self) -> None:
        """Telnyx's buffer was cleared: the next frame goes out at once and the lead restarts at 0."""
        self._anchor = None
        self._sent = 0
        self._gap = True

    def throttle(self, now_ms: float) -> None:
        """Telnyx rate limit (``100005``): pause 100 ms and keep a 1-frame lead for 5 s."""
        self._paused_until = now_ms + RATE_LIMIT_PAUSE_MS
        self._throttled_until = now_ms + RATE_LIMIT_WINDOW_MS

    def _nominal(self, anchor: float, now_ms: float) -> float:
        return anchor + max(0, self._sent - self.lead_frames_at(now_ms)) * self._frame_ms


class OutboundSink(Protocol):
    """Where wire frames and marks go (the transport encodes them as Telnyx/widget messages)."""

    async def send_audio(self, frame: bytes) -> None:
        """Send one 20 ms wire frame."""
        ...

    async def send_mark(self, name: str) -> None:
        """Send a playback mark (echoed by the far end once everything before it has played)."""
        ...


@dataclass(frozen=True, slots=True)
class SendTiming:
    """How the transport spent the last ``send_audio`` await (ms)."""

    lock_wait_ms: float
    """Waiting for the socket behind another send (a mark, a clear)."""
    socket_ms: float
    """The socket write itself."""


@runtime_checkable
class TimedSink(Protocol):
    """An :class:`OutboundSink` that reports the timing of its last audio send (underrun attribution)."""

    @property
    def last_audio_timing(self) -> SendTiming | None:
        """Timing of the most recent ``send_audio`` (``None`` before the first)."""
        ...


@dataclass(slots=True)
class OutboundStats:
    """Counters of one outbound stream."""

    frames_sent: int = 0
    speech_frames: int = 0
    idle_frames: int = 0
    marks_sent: int = 0
    late_frames: int = 0
    max_lateness_ms: float = 0.0
    dropped_frames: int = 0
    underruns: int = 0
    underruns_logged: int = 0
    max_underrun_wake_late_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class _Frame:
    tracker: PlaybackTracker
    data: bytes


@dataclass(frozen=True, slots=True)
class _SegmentEnd:
    tracker: PlaybackTracker
    chars: int


@dataclass(frozen=True, slots=True)
class _Finish:
    tracker: PlaybackTracker


type _Item = _Frame | _SegmentEnd | _Finish


class OutboundAudio:
    """The outbound clock of one leg: paced 20 ms frames, background bed, marks and playback tracking."""

    __slots__ = (
        "_clock",
        "_continuing",
        "_controls_await_ms",
        "_due_ms",
        "_framer",
        "_generation",
        "_last_logged_ms",
        "_last_logged_tracker",
        "_last_send_await_ms",
        "_last_sent_ms",
        "_last_timing",
        "_mixer",
        "_on_finished",
        "_on_underrun",
        "_open",
        "_pacer",
        "_pending_chars",
        "_queue",
        "_queued_frames",
        "_segment_frames",
        "_sink",
        "_stats",
        "_suppressed",
        "_timed",
        "_wake",
    )

    def __init__(  # noqa: PLR0913 (collaborators of one leg; everything after the mixer is keyword-only)
        self,
        sink: OutboundSink,
        mixer: FrameMixer,
        *,
        clock: Clock,
        pacer: FramePacer | None = None,
        on_finished: Callable[[PlaybackTracker], None] | None = None,
        on_underrun: Callable[[], None] | None = None,
    ) -> None:
        self._sink = sink
        self._on_finished = on_finished
        self._on_underrun = on_underrun
        self._continuing: PlaybackTracker | None = None
        self._mixer = mixer
        self._clock = clock
        self._pacer = pacer if pacer is not None else FramePacer()
        self._framer = Framer(mixer.format)
        self._queue: deque[_Item] = deque()
        self._queued_frames = 0
        self._open: PlaybackTracker | None = None
        self._segment_frames = 0
        self._pending_chars = 0
        self._generation = 0
        self._stats = OutboundStats()
        self._wake = asyncio.Event()
        self._due_ms: float | None = None
        self._last_sent_ms: float | None = None
        self._last_send_await_ms = 0.0
        self._timed = sink if isinstance(sink, TimedSink) else None
        self._last_timing: SendTiming | None = None
        self._controls_await_ms = 0.0
        self._last_logged_tracker: PlaybackTracker | None = None
        self._last_logged_ms: float | None = None
        self._suppressed = 0

    @property
    def stats(self) -> OutboundStats:
        return self._stats

    @property
    def pacer(self) -> FramePacer:
        return self._pacer

    @property
    def tracker(self) -> PlaybackTracker | None:
        """The utterance currently accepting audio."""
        return self._open

    @property
    def buffered_frames(self) -> int:
        """Speech frames queued locally (not yet sent)."""
        return self._queued_frames

    @property
    def buffered_ms(self) -> int:
        return self._queued_frames * FRAME_MS

    def start_utterance(self, tracker: PlaybackTracker) -> None:
        """Following audio belongs to ``tracker`` (the previous utterance must be finished or cleared)."""
        if self._open is not None:
            message = "the previous utterance is still open: finish_utterance or clear first"
            raise RuntimeError(message)
        self._open = tracker
        self._framer.reset()
        self._segment_frames = 0
        self._pending_chars = 0

    def push_audio(self, chunk: bytes) -> None:
        """Queue TTS audio of any size (re-framed to 20 ms; a partial frame waits for more)."""
        tracker = self._require_open()
        for frame in self._framer.feed(chunk):
            self._queue.append(_Frame(tracker, frame))
            self._queued_frames += 1
            self._segment_frames += 1
        self._wake.set()

    def end_segment(self, chars: int) -> None:
        """Close the segment queued since the last boundary; its mark follows its last frame.

        A segment without whole frames carries its characters into the next one (a segment never is empty).
        """
        tracker = self._require_open()
        if self._segment_frames == 0:
            self._pending_chars += chars
            return
        self._queue.append(_SegmentEnd(tracker, self._pending_chars + chars))
        self._segment_frames = 0
        self._pending_chars = 0
        self._wake.set()

    def finish_utterance(self, chars: int = 0) -> None:
        """No more audio: pad the last partial frame, close the trailing segment, then finish the tracker.

        ``chars`` are added to the trailing segment (dropped when no frame is left to carry them).
        """
        tracker = self._require_open()
        self._pending_chars += chars
        tail = self._framer.flush()
        if tail is not None:
            self._queue.append(_Frame(tracker, tail))
            self._queued_frames += 1
            self._segment_frames += 1
        if self._segment_frames:
            self._queue.append(_SegmentEnd(tracker, self._pending_chars))
        self._queue.append(_Finish(tracker))
        self._open = None
        self._segment_frames = 0
        self._pending_chars = 0
        self._wake.set()

    def clear(self) -> int:
        """Barge-in: drop queued frames and marks (the caller stops the trackers); returns frames dropped."""
        dropped = self._queued_frames
        self._queue.clear()
        self._queued_frames = 0
        self._framer.reset()
        self._open = None
        self._segment_frames = 0
        self._pending_chars = 0
        self._generation += 1
        self._continuing = None
        self._pacer.reset()
        self._stats.dropped_frames += dropped
        return dropped

    def rate_limited(self) -> None:
        """Telnyx answered ``100005``: back off (pause, then a reduced lead)."""
        self._pacer.throttle(self._now_ms())

    async def run(self) -> None:
        """Send frames until cancelled (run it under a supervisor/task group)."""
        while True:
            await self._send_controls(self._generation)
            if self._queued_frames == 0 and not self._mixer.has_background:
                self._pacer.mark_gap()
                self._continuing = None
                self._due_ms = None
                self._wake.clear()
                await self._wake.wait()
                continue
            now = self._now_ms()
            due = self._pacer.next_send_ms(now)
            wait_ms = due - now
            if wait_ms > _DUE_EPSILON_MS:
                self._due_ms = due
                await self._clock.sleep(wait_ms / _MS_PER_SECOND)
                continue
            await self._send_frame(now)
            self._due_ms = None

    async def _send_frame(self, now: float) -> None:
        generation = self._generation
        speech, wire = self._next_wire()
        starved = speech is not None and self._continuing is speech.tracker and self._pacer.drained(now)
        fields = self._underrun_fields(now, speech.tracker) if starved and speech is not None else None
        self._controls_await_ms = 0.0
        await self._sink.send_audio(wire)
        self._last_send_await_ms = self._now_ms() - now
        self._last_timing = None if self._timed is None else self._timed.last_audio_timing
        self._last_sent_ms = now
        if generation != self._generation:
            return
        self._count(self._pacer.on_sent(now), speech=speech is not None)
        if speech is None:
            self._continuing = None
        else:
            self._continuing = speech.tracker
            if fields is not None:
                self._underrun(now, speech.tracker, fields)
            speech.tracker.frame_sent(now)
        await self._send_controls(generation)

    def _next_wire(self) -> tuple[_Frame | None, bytes]:
        """The next speech frame (taken off the queue) and its wire bytes, or idle audio if none is queued."""
        head = self._queue[0] if self._queue else None
        if isinstance(head, _Frame):
            self._queue.popleft()
            self._queued_frames -= 1
            return head, self._mixer.speech(head.data)
        return None, self._mixer.idle()

    def _underrun(self, now: float, tracker: PlaybackTracker, fields: dict[str, float | int | None]) -> None:
        self._stats.underruns += 1
        self._report_underrun(now, tracker, fields)
        if self._on_underrun is not None:
            self._on_underrun()

    async def _send_controls(self, generation: int) -> None:
        """Marks and finishes at the head of the queue, in order (stops early when a clear intervenes)."""
        while self._queue and not isinstance(self._queue[0], _Frame) and generation == self._generation:
            item = self._queue.popleft()
            if item.tracker.stopped:
                continue
            if isinstance(item, _SegmentEnd):
                name = item.tracker.end_segment(item.chars)
                started = self._now_ms()
                await self._sink.send_mark(name)
                self._controls_await_ms += self._now_ms() - started
                self._stats.marks_sent += 1
            else:
                self._continuing = None
                item.tracker.finish()
                if self._on_finished is not None:
                    self._on_finished(item.tracker)

    def _underrun_fields(self, now: float, tracker: PlaybackTracker) -> dict[str, float | int | None]:
        """The attribution of an underrun, snapshotted before its frame goes out (specs/07 §9 Decision
        P08-FIX-4): everything refers to the previous frame's send and the wait since."""
        drained_at = self._pacer.drained_at_ms
        timing = self._last_timing
        await_ms = self._last_send_await_ms
        return {
            "turn": tracker.turn,
            "behind_ms": None if drained_at is None else round(now - drained_at, 1),
            "since_prev_send_ms": None if self._last_sent_ms is None else round(now - self._last_sent_ms, 1),
            "prev_send_await_ms": round(await_ms, 1),
            "prev_lock_wait_ms": None if timing is None else round(timing.lock_wait_ms, 1),
            "prev_socket_send_ms": None if timing is None else round(timing.socket_ms, 1),
            "prev_resume_ms": (
                None
                if timing is None
                else round(max(0.0, await_ms - timing.lock_wait_ms - timing.socket_ms), 1)
            ),
            "prev_controls_await_ms": round(self._controls_await_ms, 1),
            "wake_late_ms": None if self._due_ms is None else round(now - self._due_ms, 1),
            "queued_frames": self._queued_frames,
        }

    def _report_underrun(
        self, now: float, tracker: PlaybackTracker, fields: dict[str, float | int | None]
    ) -> None:
        """Log a counted underrun, throttled per leg (the first of each utterance always goes out)."""
        stats = self._stats
        wake_late = fields["wake_late_ms"]
        if wake_late is not None:
            stats.max_underrun_wake_late_ms = max(stats.max_underrun_wake_late_ms, float(wake_late))
        recent = self._last_logged_ms is not None and now - self._last_logged_ms < UNDERRUN_LOG_INTERVAL_MS
        if tracker is self._last_logged_tracker and recent:
            self._suppressed += 1
            return
        _log.warning("outbound.underrun", suppressed=self._suppressed, **fields)
        self._suppressed = 0
        self._last_logged_tracker = tracker
        self._last_logged_ms = now
        stats.underruns_logged += 1

    def _count(self, lateness: float, *, speech: bool) -> None:
        stats = self._stats
        stats.frames_sent += 1
        if speech:
            stats.speech_frames += 1
        else:
            stats.idle_frames += 1
        if lateness > LATE_THRESHOLD_MS:
            stats.late_frames += 1
        stats.max_lateness_ms = max(stats.max_lateness_ms, lateness)

    def _require_open(self) -> PlaybackTracker:
        if self._open is None:
            message = "no open utterance: call start_utterance first"
            raise RuntimeError(message)
        return self._open

    def _now_ms(self) -> float:
        return self._clock.monotonic() * _MS_PER_SECOND
