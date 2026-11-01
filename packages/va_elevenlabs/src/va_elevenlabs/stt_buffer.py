"""Audio batching and the reconnect replay buffer of the realtime STT client (specs/08 §4, §7; Decision
P03-T11 rules 3 and 7). Pure, no I/O."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

from va_elevenlabs.stt_models import SttAudioFormat
from va_elevenlabs.stt_wire import bytes_per_ms

__all__ = ["AudioBatcher", "ReplayBuffer", "pieces"]

_MS_PER_S: Final = 1000


def pieces(data: bytes, size: int) -> Iterator[bytes]:
    """``data`` cut into ``size``-byte pieces (the last one shorter)."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


class AudioBatcher:
    """Accumulates codec frames until one message's worth (``batch_ms``) is buffered."""

    def __init__(self, fmt: SttAudioFormat, batch_ms: int) -> None:
        self._per_ms = bytes_per_ms(fmt)
        self._buffer = bytearray()
        self._batch_ms = batch_ms

    @property
    def batch_ms(self) -> int:
        return self._batch_ms

    @property
    def batch_bytes(self) -> int:
        return self._batch_ms * self._per_ms

    def resize(self, batch_ms: int) -> None:
        self._batch_ms = batch_ms

    def add(self, frame: bytes) -> None:
        self._buffer.extend(frame)

    @property
    def ready(self) -> bool:
        """Whether at least ``batch_ms`` of audio is buffered."""
        return len(self._buffer) >= self.batch_bytes

    def take(self) -> bytes:
        """Everything buffered (possibly empty); the buffer is cleared."""
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


class ReplayBuffer:
    """Audio sent since the last acknowledged commit, addressed by absolute byte offsets (marks).

    At most ``max_s`` of audio is kept; the oldest bytes are dropped first.
    """

    def __init__(self, fmt: SttAudioFormat, max_s: float) -> None:
        self._per_ms = bytes_per_ms(fmt)
        self._max_bytes = round(max_s * _MS_PER_S) * self._per_ms
        self._data = bytearray()
        self._base = 0
        self.dropped_bytes = 0

    @property
    def end(self) -> int:
        """The mark after the last appended byte."""
        return self._base + len(self._data)

    @property
    def dropped_ms(self) -> int:
        return self.dropped_bytes // self._per_ms

    def __len__(self) -> int:
        return len(self._data)

    def append(self, chunk: bytes) -> int:
        """Keep ``chunk``; returns the mark after it."""
        self._data.extend(chunk)
        excess = len(self._data) - self._max_bytes
        if excess > 0:
            self._drop(excess)
            self.dropped_bytes += excess
        return self.end

    def ack(self, mark: int) -> None:
        """Forget everything before ``mark`` (its commit's transcript arrived)."""
        self._drop(min(mark, self.end) - self._base)

    def split(self, mark: int | None) -> tuple[bytes, bytes]:
        """The kept audio before ``mark`` and after it (``None``: nothing before)."""
        cut = 0 if mark is None else max(0, min(mark, self.end) - self._base)
        return bytes(self._data[:cut]), bytes(self._data[cut:])

    def _drop(self, count: int) -> None:
        if count <= 0:
            return
        del self._data[:count]
        self._base += count
