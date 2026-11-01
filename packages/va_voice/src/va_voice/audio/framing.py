"""Re-framing of arbitrary byte chunks into exact 20 ms frames (specs/07-media-pipeline.md §4).

Telnyx may deliver media in sizes other than 20 ms and ElevenLabs TTS audio arrives in arbitrary chunks; the
``Framer`` buffers bytes and hands out whole frames. ``flush`` pads the remainder with codec silence (0xFF for
μ-law, zeros for L16/PCM16).
"""

from __future__ import annotations

from collections.abc import Iterable

from va_voice.audio.formats import AudioFormat

__all__ = ["Framer", "join_frames", "split_frames"]


class Framer:
    """Collects byte chunks of one format and yields exact 20 ms frames."""

    __slots__ = ("_buffer", "_format")

    def __init__(self, fmt: AudioFormat) -> None:
        self._format = fmt
        self._buffer = bytearray()

    @property
    def format(self) -> AudioFormat:
        return self._format

    @property
    def pending(self) -> int:
        """Bytes buffered that do not yet form a whole frame."""
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[bytes]:
        """Buffer ``chunk`` and return every whole frame now available (possibly none)."""
        self._buffer += chunk
        size = self._format.frame_bytes
        whole = len(self._buffer) - len(self._buffer) % size
        if not whole:
            return []
        view = memoryview(self._buffer)
        try:
            frames = [bytes(view[i : i + size]) for i in range(0, whole, size)]
        finally:
            view.release()
        del self._buffer[:whole]
        return frames

    def flush(self) -> bytes | None:
        """The remainder padded with codec silence to one frame (``None`` when empty); empties the buffer."""
        if not self._buffer:
            return None
        missing = self._format.frame_bytes - len(self._buffer)
        frame = bytes(self._buffer) + bytes([self._format.silence_byte]) * missing
        self._buffer.clear()
        return frame

    def reset(self) -> None:
        """Drop buffered bytes without emitting them (e.g. after a playback clear)."""
        self._buffer.clear()


def split_frames(data: bytes, fmt: AudioFormat) -> tuple[list[bytes], bytes]:
    """``data`` cut into whole frames of ``fmt`` plus the unframed remainder."""
    size = fmt.frame_bytes
    whole = len(data) - len(data) % size
    return [data[i : i + size] for i in range(0, whole, size)], data[whole:]


def join_frames(frames: Iterable[bytes]) -> bytes:
    """Concatenate frames (e.g. five 20 ms frames into one 100 ms Scribe chunk)."""
    return b"".join(frames)
