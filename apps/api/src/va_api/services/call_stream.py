"""``GET /v1/calls/{call_id}/events/stream``: a call's events as Server-Sent Events
(specs/19-events-and-webhooks.md §9; Decision P06-T06a).

:func:`open_call_stream` does everything that can fail before the response starts: the ownership check
(``CALL_NOT_FOUND``), the ``Last-Event-ID`` check (``INVALID_INPUT``), and the ``call:{<id>}:live``
subscription (``SERVICE_UNAVAILABLE``). :class:`CallStream` renders the shared replay-then-tail core
(:mod:`va_api.services.call_tail`, every event type) as frames: ``retry: 3000`` first, then one
``id``/``event``/``data`` frame per event, and a ``: ping`` comment after :data:`HEARTBEAT_S` without a
frame. The stream ends when the tail ends (30 s after ``call.completed``, after 4 h, overflow, lost Redis)
or when the client goes away.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Final

from fastapi import Request

from va_api.auth.context import RequestContext
from va_api.services.call_tail import (
    CLOSE_AFTER_COMPLETED_S,
    GAP_HOLD_S,
    MAX_STREAM_S,
    REPLAY_PAGE,
    CallTail,
    StreamDeps,
    call_metadata,
    subscribe_tail,
)
from va_api.sse import HEARTBEAT_FRAME, retry_frame, sse_frame
from va_common.errors import AppError
from va_common.logging import get_logger

__all__ = [
    "CLOSE_AFTER_COMPLETED_S",
    "GAP_HOLD_S",
    "HEARTBEAT_S",
    "MAX_STREAM_S",
    "REPLAY_PAGE",
    "RETRY_MS",
    "CallStream",
    "StreamDeps",
    "open_call_stream",
    "parse_last_event_id",
    "stream_deps_of",
]

HEARTBEAT_S: Final = 15.0
RETRY_MS: Final = 3_000
LAST_EVENT_ID_FIELD: Final = "Last-Event-ID"
LAST_EVENT_ID_MESSAGE: Final = "Last-Event-ID must be the non-negative integer id of a received event."

_log = get_logger(__name__)


def stream_deps_of(request: Request) -> StreamDeps:
    """The stream dependencies of the running app."""
    container = request.app.state.container
    return StreamDeps(database=container.database, live_hub=container.live_hub, clock=container.clock)


def parse_last_event_id(value: str | None) -> int:
    """The seq to resume after (``0`` without the header); ``INVALID_INPUT`` unless a non-negative integer."""
    if value is None or not value.strip():
        return 0
    text = value.strip()
    if not text.isascii() or not text.isdigit():
        raise AppError("INVALID_INPUT", LAST_EVENT_ID_MESSAGE, field=LAST_EVENT_ID_FIELD)
    return int(text)


async def open_call_stream(
    deps: StreamDeps, ctx: RequestContext, call_id: uuid.UUID, last_event_id: str | None
) -> CallStream:
    """Check the call and the header, subscribe the live channel, and return the stream (not started)."""
    metadata = await call_metadata(deps, ctx.org_id, call_id)
    after = parse_last_event_id(last_event_id)
    tail = await subscribe_tail(
        deps,
        org_id=ctx.org_id,
        call_id=call_id,
        metadata=metadata,
        after=after,
        types=None,
        heartbeat_s=HEARTBEAT_S,
    )
    return CallStream(tail, call_id)


class CallStream:
    """One client's stream of one call; iterate :meth:`frames` once, :meth:`aclose` releases it."""

    def __init__(self, tail: CallTail, call_id: uuid.UUID) -> None:
        self._tail = tail
        self._call_id = call_id
        self._sent = 0
        self._closed = False

    async def frames(self) -> AsyncGenerator[bytes]:
        """``retry``, then one frame per event of the tail (``: ping`` when a heartbeat is due)."""
        try:
            yield retry_frame(RETRY_MS)
            async for item in self._tail.items():
                if item is None:
                    yield HEARTBEAT_FRAME
                    continue
                self._sent += 1
                yield sse_frame(item.seq, item.event_type, item.body)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Release the live subscription (idempotent) and log how the stream ended."""
        if self._closed:
            return
        self._closed = True
        await self._tail.aclose()
        _log.info(
            "calls.event_stream_closed",
            call_id=str(self._call_id),
            reason=self._tail.reason,
            frames=self._sent,
        )
