"""The Telnyx media WebSocket as the pipeline's :class:`~va_voice.pipeline.protocols.Transport`
(specs/06-telephony-telnyx.md §8; specs/06 §8 Decision P05-T08).

:class:`TelnyxMediaTransport` takes over the session's socket after ``start``. Inbound, it turns ``media``
into :class:`InboundAudio` (the Telnyx ``chunk`` stays the reorder key: the pipeline's ``InboundPath``
reorders), ``dtmf`` into :class:`InboundDtmf`, and ``mark`` into :class:`MarkEcho`; ``stop`` or a peer close
ends the iterator. Frames that are not caller audio (another ``track``, an empty payload, an odd-length L16
payload) are dropped and counted, malformed messages are logged and skipped, and Telnyx ``error`` frames are
counted (a ``100005`` rate limit calls :attr:`TelnyxMediaTransport.on_rate_limited`). Outbound,
``media``/``mark``/``clear`` messages are serialized by one lock; once the stream ended every send is dropped
silently.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Final

from va_common.logging import get_logger
from va_media.session import StreamStart
from va_media.socket import MediaSocket, SocketClosedError
from va_telnyx.media import (
    InboundMediaMessage,
    MediaDtmf,
    MediaError,
    MediaErrorCode,
    MediaFormat,
    MediaFrame,
    MediaMark,
    MediaProtocolError,
    MediaStop,
    clear_message,
    mark_message,
    media_message,
    parse_media_message,
)
from va_voice.audio.formats import L16_16000, ULAW_8000, AudioFormat
from va_voice.pipeline.protocols import InboundAudio, InboundDtmf, MarkEcho, TransportEvent

__all__ = [
    "INBOUND_TRACK",
    "TelnyxMediaTransport",
    "TransportStats",
    "UnsupportedMediaFormatError",
    "leg_format",
    "stream_format",
]

INBOUND_TRACK: Final = "inbound"

_log = get_logger(__name__)

_LEG_FORMATS: Final[dict[tuple[str, int, int], AudioFormat]] = {
    ("PCMU", 8000, 1): ULAW_8000,
    ("L16", 16000, 1): L16_16000,
}

_OUR_BUGS: Final = frozenset({MediaErrorCode.MALFORMED_FRAME, MediaErrorCode.INVALID_MEDIA})


class UnsupportedMediaFormatError(ValueError):
    """The stream's ``media_format`` is neither PCMU 8 kHz nor L16 16 kHz mono."""


def stream_format(media_format: MediaFormat) -> str:
    """The stream's format as ``ENCODING/RATE`` (``"PCMU/8000"``, ``"L16/16000"``)."""
    return f"{media_format.encoding}/{media_format.sample_rate}"


def leg_format(media_format: MediaFormat) -> AudioFormat:
    """The pipeline format of a leg: PCMU/8000/1 → ``ULAW_8000``, L16/16000/1 → ``L16_16000``."""
    key = (media_format.encoding, media_format.sample_rate, media_format.channels)
    found = _LEG_FORMATS.get(key)
    if found is None:
        msg = f"unsupported media format {key[0]}/{key[1]}/{key[2]}"
        raise UnsupportedMediaFormatError(msg)
    return found


@dataclass(slots=True)
class TransportStats:
    """Counters of one media stream (never payloads or digits)."""

    frames: int = 0
    dtmf: int = 0
    marks: int = 0
    dropped_frames: Counter[str] = field(default_factory=Counter)
    protocol_errors: int = 0
    errors: Counter[int] = field(default_factory=Counter)
    sent_frames: int = 0
    sent_marks: int = 0
    clears: int = 0
    dropped_sends: int = 0

    def log_fields(self) -> dict[str, object]:
        return {
            "frames": self.frames,
            "dtmf": self.dtmf,
            "marks": self.marks,
            "dropped_frames": dict(self.dropped_frames),
            "protocol_errors": self.protocol_errors,
            "errors": {str(code): count for code, count in self.errors.items()},
            "sent_frames": self.sent_frames,
            "sent_marks": self.sent_marks,
            "clears": self.clears,
            "dropped_sends": self.dropped_sends,
        }


class TelnyxMediaTransport:
    """One Telnyx media stream after ``start``; :meth:`events` has a single consumer."""

    def __init__(
        self,
        socket: MediaSocket,
        start: StreamStart,
        *,
        on_rate_limited: Callable[[], None] | None = None,
    ) -> None:
        self._socket = socket
        self._fmt = leg_format(start.media_format)
        self._stream_id = start.stream_id
        self.on_rate_limited: Callable[[], None] | None = on_rate_limited
        """Called on each Telnyx ``100005`` error (the pipeline wires ``OutboundAudio.rate_limited``)."""
        self._stats = TransportStats()
        self._send_lock = asyncio.Lock()
        self._consumed = False
        self._stop_received = False
        self._closed_by_peer: int | None = None
        self._send_failed = False

    @property
    def fmt(self) -> AudioFormat:
        return self._fmt

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def stats(self) -> TransportStats:
        return self._stats

    @property
    def stop_received(self) -> bool:
        return self._stop_received

    @property
    def closed_by_peer(self) -> int | None:
        """The peer's close code once it closed the socket."""
        return self._closed_by_peer

    @property
    def ended(self) -> bool:
        """``stop`` arrived, the peer closed, or a send found the socket closed."""
        return self._stop_received or self._closed_by_peer is not None or self._send_failed

    # Inbound.

    def events(self) -> AsyncIterator[TransportEvent]:
        """Inbound events in arrival order until ``stop`` or a peer close; callable once."""
        if self._consumed:
            msg = "the media transport events are already being consumed"
            raise RuntimeError(msg)
        self._consumed = True
        return self._read()

    async def _read(self) -> AsyncIterator[TransportEvent]:
        while True:
            try:
                raw = await self._socket.receive()
            except SocketClosedError as exc:
                self._closed_by_peer = exc.code
                return
            try:
                message = parse_media_message(raw)
            except MediaProtocolError as exc:
                self._stats.protocol_errors += 1
                _log.warning("media.protocol_error", detail=str(exc))
                continue
            if isinstance(message, MediaStop):
                self._stop_received = True
                return
            event = self._inbound(message)
            if event is not None:
                yield event

    def _inbound(self, message: InboundMediaMessage) -> TransportEvent | None:
        if isinstance(message, MediaFrame):
            return self._audio(message)
        if isinstance(message, MediaDtmf):
            self._stats.dtmf += 1
            return InboundDtmf(message.dtmf.digit)
        if isinstance(message, MediaMark):
            self._stats.marks += 1
            return MarkEcho(message.mark.name)
        if isinstance(message, MediaError):
            self._error(message)
            return None
        event = getattr(message, "event", "unknown")
        _log.debug("media.event_ignored", media_event=event)
        return None

    def _audio(self, frame: MediaFrame) -> InboundAudio | None:
        reason = self._drop_reason(frame)
        if reason is not None:
            self._drop(reason)
            return None
        self._stats.frames += 1
        return InboundAudio(frame.chunk, frame.payload)

    def _drop_reason(self, frame: MediaFrame) -> str | None:
        if frame.track is not None and frame.track != INBOUND_TRACK:
            return "track"
        if not frame.payload:
            return "empty"
        if len(frame.payload) % self._fmt.sample_width:
            return "invalid_media"
        return None

    def _drop(self, reason: str) -> None:
        self._stats.dropped_frames[reason] += 1
        if self._stats.dropped_frames[reason] == 1:
            _log.warning("media.frame_dropped", reason=reason)

    def _error(self, message: MediaError) -> None:
        code = message.payload.code
        self._stats.errors[code] += 1
        if code == MediaErrorCode.RATE_LIMIT_REACHED:
            _log.warning("media.telnyx_rate_limited", code=code)
            if self.on_rate_limited is not None:
                self.on_rate_limited()
        elif code in _OUR_BUGS:
            _log.error("media.telnyx_error", code=code, title=message.payload.title)
        else:
            _log.warning("media.telnyx_error", code=code, title=message.payload.title)

    # Outbound.

    async def send_audio(self, frame: bytes) -> None:
        """Send one wire frame as a ``media`` message; an empty frame is a ``ValueError``."""
        if await self._send(media_message(frame)):
            self._stats.sent_frames += 1

    async def send_mark(self, name: str) -> None:
        if await self._send(mark_message(name)):
            self._stats.sent_marks += 1

    async def clear(self) -> None:
        if await self._send(clear_message()):
            self._stats.clears += 1

    async def _send(self, message: bytes) -> bool:
        async with self._send_lock:
            if self.ended:
                self._stats.dropped_sends += 1
                return False
            try:
                await self._socket.send_text(message.decode("ascii"))
            except SocketClosedError:
                self._send_failed = True
                self._stats.dropped_sends += 1
                return False
            return True

    def log_summary(self) -> None:
        """Log the stream's counters once it ended (``media.transport_closed``)."""
        _log.info(
            "media.transport_closed",
            stop_received=self._stop_received,
            closed_by_peer=self._closed_by_peer,
            **self._stats.log_fields(),
        )
