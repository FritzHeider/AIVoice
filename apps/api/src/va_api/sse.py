"""Server-Sent Events framing and the streaming response of the live streams (specs/19-events-and-webhooks.md
§9; Decision P06-T06a).

A frame is ``id: <seq>``, an optional ``event: <type>``, and one ``data:`` line holding single-line JSON,
ended by a blank line. :class:`EventStreamResponse` streams frames with ``Cache-Control: no-store`` and
``X-Accel-Buffering: no`` and always runs its ``on_close`` callback when the response ends, however it ends
(completed, client gone, cancelled): Starlette does not close an abandoned body iterator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Final

from fastapi.responses import StreamingResponse
from pydantic import JsonValue
from starlette.types import Receive, Scope, Send

from va_common.json import dumps

__all__ = ["EVENT_STREAM_HEADERS", "HEARTBEAT_FRAME", "EventStreamResponse", "retry_frame", "sse_frame"]

MEDIA_TYPE: Final = "text/event-stream"
HEARTBEAT_FRAME: Final = b": ping\n\n"
EVENT_STREAM_HEADERS: Final[Mapping[str, str]] = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}


def sse_frame(seq: int, event_type: str | None, body: Mapping[str, JsonValue]) -> bytes:
    """One frame; without ``event_type`` the ``event:`` line is left out (EventSource type ``message``)."""
    event_line = b"" if event_type is None else b"event: " + event_type.encode() + b"\n"
    return b"id: %d\n" % seq + event_line + b"data: " + dumps(dict(body)) + b"\n\n"


def retry_frame(milliseconds: int) -> bytes:
    """The reconnection delay a client should use."""
    return b"retry: %d\n\n" % milliseconds


class EventStreamResponse(StreamingResponse):
    """A ``text/event-stream`` response over ``frames`` that awaits ``on_close`` when it ends."""

    def __init__(
        self, frames: AsyncIterator[bytes], on_close: Callable[[], Awaitable[None]], status_code: int = 200
    ) -> None:
        # ``status_code`` is read from the signature by FastAPI's OpenAPI generator.
        super().__init__(
            frames, status_code=status_code, media_type=MEDIA_TYPE, headers=dict(EVENT_STREAM_HEADERS)
        )
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._on_close()
