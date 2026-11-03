"""The replay-then-tail core shared by a call's live streams (specs/19-events-and-webhooks.md §9; Decisions
P06-T06a and P06-T06b).

:func:`call_metadata` checks that the call belongs to the org (``CALL_NOT_FOUND``) and loads its metadata;
:func:`subscribe_tail` subscribes ``call:{<id>}:live`` on the process's :class:`~va_events.live_hub.LiveHub`
(``SERVICE_UNAVAILABLE``). The subscription comes first, then :meth:`CallTail.items` replays the stored
``call_events`` rows with ``seq`` above ``after`` (pages of 500) and tails the live events, so nothing
published after the replay read is missed. Items go out strictly in increasing ``seq``; a live event that
leaves a gap is held up to :data:`GAP_HOLD_S` for the missing seqs (live, or re-read from ``call_events``),
then the remaining holes are skipped. Only events that have a ``call_events`` row are items, so replay and
tail agree.

``types`` limits the items to those event types; the other events still take their place in the seq order
(a missing one is waited for like any other), and ``call.completed`` drives the close even when it is not an
item. The tail ends :data:`CLOSE_AFTER_COMPLETED_S` after ``call.completed`` (at once for an older call),
after :data:`MAX_STREAM_S`, or when the subscription overflows or loses Redis; with ``heartbeat_s`` a
``None`` item asks the renderer for a heartbeat after that long without an item. All times are on the
injected clock.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from pydantic import JsonValue, ValidationError

from va_api.services.call_read import org_call
from va_api.services.call_stream_order import SeqOrder
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
    "MAX_STREAM_S",
    "REPLAY_PAGE",
    "CallTail",
    "StreamDeps",
    "TailItem",
    "call_metadata",
    "subscribe_tail",
]

CLOSE_AFTER_COMPLETED_S: Final = 30.0
GAP_HOLD_S: Final = 2.0
MAX_STREAM_S: Final = 14_400.0
REPLAY_PAGE: Final = 500
UNAVAILABLE_MESSAGE: Final = "The live event stream is temporarily unavailable; retry shortly."
_COMPLETED: Final = EventType.CALL_COMPLETED.value

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StreamDeps:
    """What a call's live stream needs from the container."""

    database: Database
    live_hub: LiveHub
    clock: Clock


@dataclass(frozen=True, slots=True)
class TailItem:
    """One event to send: its seq, its type (``None`` for rows written before migration 0007), and its
    bland-live body (specs/19 §5)."""

    seq: int
    event_type: str | None
    body: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _Slot:
    """A seq's item (``None`` when filtered out) and, for ``call.completed``, when the event happened."""

    item: TailItem | None
    completed_at: datetime | None


async def call_metadata(deps: StreamDeps, org_id: uuid.UUID, call_id: uuid.UUID) -> dict[str, JsonValue]:
    """The call's metadata; ``CALL_NOT_FOUND`` unless the call belongs to ``org_id``."""
    async with deps.database.tenant_session(org_id) as session:
        call = await org_call(session, org_id, call_id)
        return dict(call.metadata_ or {})


async def subscribe_tail(  # noqa: PLR0913 - the tail's facts
    deps: StreamDeps,
    *,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    metadata: Mapping[str, JsonValue],
    after: int,
    types: frozenset[str] | None,
    heartbeat_s: float | None,
) -> CallTail:
    """Subscribe the call's live channel and return the tail (not started); ``SERVICE_UNAVAILABLE`` when the
    hub cannot subscribe."""
    try:
        subscription = await deps.live_hub.subscribe(call_live_channel(call_id))
    except LiveHubError as exc:
        raise AppError("SERVICE_UNAVAILABLE", UNAVAILABLE_MESSAGE) from exc
    return CallTail(
        deps,
        org_id=org_id,
        call_id=call_id,
        metadata=metadata,
        after=after,
        sub=subscription,
        types=types,
        heartbeat_s=heartbeat_s,
    )


class CallTail:
    """One client's tail of one call; iterate :meth:`items` once, :meth:`aclose` releases it."""

    def __init__(  # noqa: PLR0913 - the opened tail's facts
        self,
        deps: StreamDeps,
        *,
        org_id: uuid.UUID,
        call_id: uuid.UUID,
        metadata: Mapping[str, JsonValue],
        after: int,
        sub: LiveSubscription,
        types: frozenset[str] | None,
        heartbeat_s: float | None,
    ) -> None:
        self._deps = deps
        self._clock = deps.clock
        self._org_id = org_id
        self._call_id = call_id
        self._metadata = metadata
        self._sub = sub
        self._types = types
        self._heartbeat_s = heartbeat_s
        self._order: SeqOrder[_Slot] = SeqOrder(after)
        self._close_at: float | None = None
        self._gap_since: float | None = None
        self._closed = False
        self.reason = "client_gone"

    async def items(self) -> AsyncGenerator[TailItem | None]:
        """The replayed items, then the live tail; ``None`` = a heartbeat is due (module docstring)."""
        started = self._clock.monotonic()
        try:
            async for slot in self._replay():
                item = self._deliver(slot)
                if item is not None:
                    yield item
            last_sent = self._clock.monotonic()
            while True:
                try:
                    message = await self._next_live(self._wait_s(started, last_sent))
                except LiveSubscriptionClosedError as closed:
                    self.reason = closed.reason
                    return
                released = [] if message is None else self._accept(message)
                if self._gap_expired():
                    released += await self._fill_gap()
                items = self._deliver_all(released)
                for item in items:
                    yield item
                now = self._clock.monotonic()
                if items:
                    last_sent = now
                if self._ended(now, started):
                    return
                if self._heartbeat_s is not None and now >= last_sent + self._heartbeat_s:
                    yield None
                    last_sent = now
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Release the live subscription (idempotent)."""
        if self._closed:
            return
        self._closed = True
        await self._deps.live_hub.unsubscribe(self._sub)

    def _ended(self, now: float, started: float) -> bool:
        """Whether the tail is over at ``now`` (sets :attr:`reason`)."""
        if self._close_at is not None and now >= self._close_at:
            self.reason = "completed"
            return True
        if now >= started + MAX_STREAM_S:
            self.reason = "max_duration"
            return True
        return False

    def _deliver_all(self, slots: list[_Slot]) -> list[TailItem]:
        """The items of the released ``slots`` (the close deadline noted on the way)."""
        return [item for item in map(self._deliver, slots) if item is not None]

    def _deliver(self, slot: _Slot) -> TailItem | None:
        if slot.completed_at is not None and self._close_at is None:
            remaining = slot.completed_at + timedelta(seconds=CLOSE_AFTER_COMPLETED_S) - self._clock.now()
            self._close_at = self._clock.monotonic() + max(remaining.total_seconds(), 0.0)
        return slot.item

    def _slot(
        self, seq: int, event_type: str | None, body: Mapping[str, JsonValue], occurred_at: datetime
    ) -> _Slot:
        completed = occurred_at if event_type == _COMPLETED else None
        wanted = self._types is None or event_type in self._types
        return _Slot(TailItem(seq, event_type, body) if wanted else None, completed)

    def _row_slot(self, row: CallEvent) -> _Slot:
        return self._slot(row.seq, row.event_type, row_body(row, self._metadata), row.created_at)

    def _wait_s(self, started: float, last_sent: float) -> float:
        deadlines = [started + MAX_STREAM_S]
        if self._heartbeat_s is not None:
            deadlines.append(last_sent + self._heartbeat_s)
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

    async def _replay(self) -> AsyncIterator[_Slot]:
        async for row in self._stored(after=self._order.last, below=None):
            self._order.advance(row.seq)
            yield self._row_slot(row)

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

    def _accept(self, message: bytes) -> list[_Slot]:
        try:
            event = decode(message)
        except (ValidationError, ValueError):
            _log.warning("calls.event_stream_undecodable", call_id=str(self._call_id))
            return []
        if event.call_id != self._call_id or event.seq is None:
            return []
        slot = None
        if row_for(event) is not None:
            slot = self._slot(
                event.seq, event.type.value, live_body(event, self._metadata), event.occurred_at
            )
        released = self._order.offer(event.seq, slot)
        self._track_gap()
        return released

    def _track_gap(self) -> None:
        if not self._order.has_gap:
            self._gap_since = None
        elif self._gap_since is None:
            self._gap_since = self._clock.monotonic()

    def _gap_expired(self) -> bool:
        return self._gap_since is not None and self._clock.monotonic() >= self._gap_since + GAP_HOLD_S

    async def _fill_gap(self) -> list[_Slot]:
        """Re-read the missing seqs below the lowest held one, then skip the holes that remain."""
        lowest = self._order.lowest_pending
        if lowest is None:
            return []
        released: list[_Slot] = []
        async for row in self._stored(after=self._order.last, below=lowest):
            released += self._order.offer(row.seq, self._row_slot(row))
        released += self._order.advance(lowest - 1)
        self._gap_since = None
        self._track_gap()
        return released
