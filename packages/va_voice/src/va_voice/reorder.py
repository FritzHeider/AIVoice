"""Inbound audio reorder buffer (specs/07-media-pipeline.md §5.1).

Telnyx media frames carry a sequence number (``media.chunk``, starting at 1) and may arrive out of order.
The buffer is transport-agnostic: callers pass the chunk number and the frame payload and get back zero or
more frames in chunk order. A missing chunk is replaced by one silence frame as soon as ``window`` later
chunks are held (≈ 100 ms at 20 ms frames). Duplicates and frames older than the last emitted chunk are
dropped. The logic is purely count-based: Telnyx streams frames continuously (silence included), so no
wall-clock timer is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol

from va_voice.audio.formats import ULAW_8000, AudioFormat

DEFAULT_WINDOW: Final = 5
DEFAULT_MAX_GAP: Final = 50


@dataclass(frozen=True, slots=True)
class OrderedFrame:
    """One frame in chunk order; ``filled`` marks synthetic silence for a chunk that never arrived."""

    chunk: int
    payload: bytes
    filled: bool


@dataclass(slots=True)
class ReorderStats:
    """Running counters of one buffer (one inbound stream)."""

    received: int = 0
    emitted: int = 0
    reordered: int = 0
    gap_frames: int = 0
    duplicates: int = 0
    resyncs: int = 0


class ReorderObserver(Protocol):
    """Receives counter increments (metrics); every method is called synchronously from ``push``/``drain``."""

    def reordered(self, count: int) -> None: ...

    def gap_filled(self, count: int) -> None: ...

    def duplicate(self, count: int) -> None: ...


class _NullObserver:
    def reordered(self, count: int) -> None:
        del count

    def gap_filled(self, count: int) -> None:
        del count

    def duplicate(self, count: int) -> None:
        del count


NULL_OBSERVER: Final[ReorderObserver] = _NullObserver()


class ReorderBuffer:
    """Chunk-keyed reorder buffer with silence gap fill.

    ``window``: number of held later chunks that declares the expected chunk lost. ``first_chunk``: number of
    the first chunk of the stream. ``max_gap``: a hole wider than this many chunks is skipped without silence
    (resync), so a sequence-number jump never floods the pipeline with fill frames.
    """

    def __init__(
        self,
        fmt: AudioFormat = ULAW_8000,
        *,
        window: int = DEFAULT_WINDOW,
        first_chunk: int = 1,
        max_gap: int = DEFAULT_MAX_GAP,
        observer: ReorderObserver = NULL_OBSERVER,
    ) -> None:
        if window < 1:
            msg = f"window must be at least 1, got {window}"
            raise ValueError(msg)
        if max_gap < window:
            msg = f"max_gap ({max_gap}) must be at least window ({window})"
            raise ValueError(msg)
        self._window = window
        self._max_gap = max_gap
        self._observer = observer
        self._expected = first_chunk
        self._held: dict[int, bytes] = {}
        self._silence = bytes([fmt.silence_byte]) * fmt.frame_bytes
        self._stats = ReorderStats()

    @property
    def expected(self) -> int:
        """Number of the next chunk to emit."""
        return self._expected

    @property
    def held(self) -> int:
        """Number of frames waiting for an earlier chunk."""
        return len(self._held)

    @property
    def stats(self) -> ReorderStats:
        return self._stats

    def push(self, chunk: int, payload: bytes) -> list[OrderedFrame]:
        """Accept one inbound frame; return the frames that are now ready, in chunk order."""
        self._stats.received += 1
        if chunk < self._expected or chunk in self._held:
            self._stats.duplicates += 1
            self._observer.duplicate(1)
            return []
        out: list[OrderedFrame] = []
        if chunk == self._expected:
            self._emit(out, chunk, payload, filled=False)
            self._release_consecutive(out)
            return out
        self._held[chunk] = payload
        self._stats.reordered += 1
        self._observer.reordered(1)
        while len(self._held) >= self._window:
            self._skip_gap(out)
        return out

    def drain(self) -> list[OrderedFrame]:
        """End of stream: emit every held frame in order, filling interior gaps (resync rule applies)."""
        out: list[OrderedFrame] = []
        while self._held:
            self._skip_gap(out)
        return out

    def _skip_gap(self, out: list[OrderedFrame]) -> None:
        """Resolve the hole at ``expected`` (held is non-empty and does not contain ``expected``)."""
        earliest = min(self._held)
        if earliest - self._expected > self._max_gap:
            self._expected = earliest
            self._stats.resyncs += 1
        else:
            self._emit(out, self._expected, self._silence, filled=True)
            self._stats.gap_frames += 1
            self._observer.gap_filled(1)
        self._release_consecutive(out)

    def _release_consecutive(self, out: list[OrderedFrame]) -> None:
        while (payload := self._held.pop(self._expected, None)) is not None:
            self._emit(out, self._expected, payload, filled=False)

    def _emit(self, out: list[OrderedFrame], chunk: int, payload: bytes, *, filled: bool) -> None:
        out.append(OrderedFrame(chunk, payload, filled))
        self._stats.emitted += 1
        self._expected = chunk + 1
