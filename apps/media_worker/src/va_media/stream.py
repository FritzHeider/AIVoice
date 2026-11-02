"""Telnyx media stream reading before the transport takes over (specs/07-media-pipeline.md §3 step 4).

:func:`wait_for_start` reads messages until Telnyx sends ``start`` (a ``connected`` first is optional), within
5 s of the injected clock. :func:`read_until_stop` is the default session body until the Telnyx transport and
the pipeline replace it: it reads and parses messages and ends the session with ``stream_closed`` when Telnyx
sends ``stop`` or closes the socket, reading through :class:`~va_media.transport.TelnyxMediaTransport`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

from va_common.logging import get_logger
from va_common.time import Clock
from va_media.session import STREAM_CLOSED, SessionEnd, StreamStart
from va_media.socket import MediaSocket, SocketClosedError
from va_media.transport import TelnyxMediaTransport
from va_telnyx.media import MediaProtocolError, MediaStart, MediaStop, parse_media_message
from va_voice.pipeline.waits import within

if TYPE_CHECKING:
    from va_media.session import CallSession

__all__ = ["MEDIA_START_TIMEOUT_S", "StreamStartError", "read_until_stop", "wait_for_start"]

MEDIA_START_TIMEOUT_S: Final = 5.0

_log = get_logger(__name__)

StartFailure = Literal["timeout", "stopped", "protocol", "disconnected"]


class StreamStartError(Exception):
    """No ``start`` frame: ``timeout`` (5 s), ``stopped`` (``stop`` first), ``protocol`` (malformed
    message), or ``disconnected`` (the peer closed)."""

    def __init__(self, reason: StartFailure) -> None:
        super().__init__(f"media stream did not start: {reason}")
        self.reason: StartFailure = reason


async def _read_start(socket: MediaSocket) -> StreamStart:
    while True:
        try:
            raw = await socket.receive()
        except SocketClosedError:
            raise StreamStartError("disconnected") from None
        try:
            message = parse_media_message(raw)
        except MediaProtocolError:
            raise StreamStartError("protocol") from None
        if isinstance(message, MediaStart):
            return StreamStart(
                stream_id=message.stream_id,
                call_control_id=message.start.call_control_id,
                media_format=message.start.media_format,
            )
        if isinstance(message, MediaStop):
            raise StreamStartError("stopped")


async def wait_for_start(
    socket: MediaSocket, clock: Clock, timeout_s: float = MEDIA_START_TIMEOUT_S
) -> StreamStart:
    """The stream's ``start`` facts; raises :class:`StreamStartError`."""
    started: list[StreamStart] = []

    async def read() -> None:
        started.append(await _read_start(socket))

    if not await within(read(), clock, timeout_s):
        raise StreamStartError("timeout")
    return started[0]


async def read_until_stop(session: CallSession) -> SessionEnd:
    """Read the stream through the transport until ``stop`` or a disconnect (inbound events are discarded)."""
    transport = TelnyxMediaTransport(session.socket, session.start)
    events = 0
    async for _event in transport.events():
        events += 1
    transport.log_summary()
    _log.debug("media.stream_read", events=events)
    return STREAM_CLOSED
