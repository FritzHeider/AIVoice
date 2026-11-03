"""``GET /v1/calls/{call_id}/events/stream``: a call's events as Server-Sent Events
(specs/19-events-and-webhooks.md §9; Decision P06-T06a).

:func:`open_call_stream` does everything that can fail before the response starts: the ownership check
(``CALL_NOT_FOUND``), the ``Last-Event-ID`` check (``INVALID_INPUT``), and the ``call:{<id>}:live``
subscription on the process's :class:`~va_events.live_hub.LiveHub` (``SERVICE_UNAVAILABLE``). The
subscription comes first, then :meth:`CallStream.frames` replays the stored ``call_events`` rows with
``seq`` above ``Last-Event-ID`` (pages of 500) and tails the live events, so nothing published after the
replay read is missed. Frames go out strictly in increasing ``seq``; a live event that leaves a gap is held
up to :data:`GAP_HOLD_S` for the missing seqs (live, or re-read from ``call_events``), then the remaining
holes are skipped. Only events that have a ``call_events`` row are streamed, so replay and tail agree.

The stream ends :data:`CLOSE_AFTER_COMPLETED_S` after ``call.completed`` (at once for an older call), after
:data:`MAX_STREAM_S`, when the subscription overflows or loses Redis, or when the client goes away; a
``: ping`` comment is sent after :data:`HEARTBEAT_S` without a frame. All times are on the injected clock.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from fastapi import Request
from pydantic import JsonValue, ValidationError

from va_api.auth.context import RequestContext
from va_api.services.call_read import owned_call
from va_api.services.call_stream_order import SeqOrder
from va_api.sse import HEARTBEAT_FRAME, retry_frame, sse_frame
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import within
from va_db.call_events_read import load_call_events
from va_db.models import CallEvent
from va_db.session import Database
from va_events import (
    EventType,
    LiveHub,
    LiveHubError,
    LiveSubscription,
    LiveSubscriptionClosedError,
    decode,
    live_body,
    row_body,
    row_for,
)
from va_events.keys import call_live_channel

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
CLOSE_AFTER_COMPLETED_S: Final = 30.0
GAP_HOLD_S: Final = 2.0
MAX_STREAM_S: Final = 14_400.0
REPLAY_PAGE: Final = 500
RETRY_MS: Final = 3_000
LAST_EVENT_ID_FIELD: Final = "Last-Event-ID"
LAST_EVENT_ID_MESSAGE: Final = "Last-Event-ID must be the non-negative integer id of a received event."
UNAVAILABLE_MESSAGE: Final = "The live event stream is temporarily unavailable; retry shortly."
_COMPLETED: Final = EventType.CALL_COMPLETED.value

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StreamDeps:
    """What a call event stream needs from the container."""

    database: Database
    live_hub: LiveHub
    clock: Clock


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


@dataclass(frozen=True, slots=True)
class _Frame:
    """An encoded frame and, for ``call.completed``, when the event happened."""

    data: bytes
    completed_at: datetime | None


def _row_frame(row: CallEvent, metadata: Mapping[str, JsonValue]) -> _Frame:
    completed = row.created_at if row.event_type == _COMPLETED else None
    return _Frame(sse_frame(row.seq, row.event_type, row_body(row, metadata)), completed)


async def open_call_stream(
    deps: StreamDeps, ctx: RequestContext, call_id: uuid.UUID, last_event_id: str | None
) -> CallStream:
    """Check the call and the header, subscribe the live channel, and return the stream (not started)."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        call = await owned_call(session, ctx, call_id)
        metadata: dict[str, JsonValue] = dict(call.metadata_ or {})
    after = parse_last_event_id(last_event_id)
    try:
        subscription = await deps.live_hub.subscribe(call_live_channel(call_id))
    except LiveHubError as exc:
        raise AppError("SERVICE_UNAVAILABLE", UNAVAILABLE_MESSAGE) from exc
    return CallStream(
        deps, org_id=ctx.org_id, call_id=call_id, metadata=metadata, after=after, sub=subscription
    )


class CallStream:
    """One client's stream of one call; iterate :meth:`frames` once, :meth:`aclose` releases it."""

    def __init__(  # noqa: PLR0913 - the opened stream's facts
        self,
        deps: StreamDeps,
        *,
        org_id: uuid.UUID,
        call_id: uuid.UUID,
        metadata: Mapping[str, JsonValue],
        after: int,
        sub: LiveSubscription,
    ) -> None:
        self._deps = deps
        self._clock = deps.clock
        self._org_id = org_id
        self._call_id = call_id
        self._metadata = metadata
        self._sub = sub
        self._order: SeqOrder[_Frame] = SeqOrder(after)
        self._sent = 0
        self._close_at: float | None = None
        self._gap_since: float | None = None
        self._reason = "client_gone"
        self._closed = False

    async def frames(self) -> AsyncGenerator[bytes]:
        """``retry``, the replayed rows, then the live tail (see the module docstring)."""
        started = self._clock.monotonic()
        last_sent = started
        try:
            yield retry_frame(RETRY_MS)
            async for frame in self._replay():
                yield self._sent_frame(frame)
            last_sent = self._clock.monotonic()
            while True:
                try:
                    message = await self._next_live(self._wait_s(started, last_sent))
                except LiveSubscriptionClosedError as closed:
                    self._reason = closed.reason
                    return
                released = [] if message is None else self._accept(message)
                if self._gap_expired():
                    released += await self._fill_gap()
                for frame in released:
                    yield self._sent_frame(frame)
                now = self._clock.monotonic()
                if released:
                    last_sent = now
                if self._close_at is not None and now >= self._close_at:
                    self._reason = "completed"
                    return
                if now >= started + MAX_STREAM_S:
                    self._reason = "max_duration"
                    return
                if now >= last_sent + HEARTBEAT_S:
                    yield HEARTBEAT_FRAME
                    last_sent = now
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Release the live subscription (idempotent) and log how the stream ended."""
        if self._closed:
            return
        self._closed = True
        await self._deps.live_hub.unsubscribe(self._sub)
        _log.info(
            "calls.event_stream_closed",
            call_id=str(self._call_id),
            reason=self._reason,
            frames=self._sent,
        )

    def _sent_frame(self, frame: _Frame) -> bytes:
        self._sent += 1
        if frame.completed_at is not None and self._close_at is None:
            remaining = frame.completed_at + timedelta(seconds=CLOSE_AFTER_COMPLETED_S) - self._clock.now()
            self._close_at = self._clock.monotonic() + max(remaining.total_seconds(), 0.0)
        return frame.data

    def _wait_s(self, started: float, last_sent: float) -> float:
        deadlines = [last_sent + HEARTBEAT_S, started + MAX_STREAM_S]
        if self._close_at is not None:
            deadlines.append(self._close_at)
        if self._gap_since is not None:
            deadlines.append(self._gap_since + GAP_HOLD_S)
        return max(min(deadlines) - self._clock.monotonic(), 0.0)

    async def _next_live(self, timeout_s: float) -> bytes | None:
        received: list[bytes] = []

        async def take() -> None:
            received.append(await self._sub.next())

        if await within(take(), self._clock, timeout_s):
            return received[0]
        return None

    async def _replay(self) -> AsyncIterator[_Frame]:
        async for row in self._stored(after=self._order.last, below=None):
            self._order.advance(row.seq)
            yield _row_frame(row, self._metadata)

    async def _stored(self, *, after: int, below: int | None) -> AsyncIterator[CallEvent]:
        """Stored rows with ``after < seq`` (and ``seq < below``), oldest first, page by page."""
        while True:
            async with self._deps.database.tenant_session(self._org_id) as session:
                rows, next_after = await load_call_events(
                    session, self._org_id, self._call_id, after_seq=after, limit=REPLAY_PAGE, categories=None
                )
            for row in rows:
                if below is not None and row.seq >= below:
                    return
                yield row
            if next_after is None:
                return
            after = next_after

    def _accept(self, message: bytes) -> list[_Frame]:
        try:
            event = decode(message)
        except (ValidationError, ValueError):
            _log.warning("calls.event_stream_undecodable", call_id=str(self._call_id))
            return []
        if event.call_id != self._call_id or event.seq is None:
            return []
        frame = None
        if row_for(event) is not None:
            completed = event.occurred_at if event.type is EventType.CALL_COMPLETED else None
            frame = _Frame(
                sse_frame(event.seq, event.type.value, live_body(event, self._metadata)), completed
            )
        released = self._order.offer(event.seq, frame)
        self._track_gap()
        return released

    def _track_gap(self) -> None:
        if not self._order.has_gap:
            self._gap_since = None
        elif self._gap_since is None:
            self._gap_since = self._clock.monotonic()

    def _gap_expired(self) -> bool:
        return self._gap_since is not None and self._clock.monotonic() >= self._gap_since + GAP_HOLD_S

    async def _fill_gap(self) -> list[_Frame]:
        """Re-read the missing seqs below the lowest held one, then skip the holes that remain."""
        lowest = self._order.lowest_pending
        if lowest is None:
            return []
        released: list[_Frame] = []
        async for row in self._stored(after=self._order.last, below=lowest):
            released += self._order.offer(row.seq, _row_frame(row, self._metadata))
        released += self._order.advance(lowest - 1)
        self._gap_since = None
        self._track_gap()
        return released
