"""``GET /v1/events/stream``: an org's new events as Server-Sent Events (specs/19-events-and-webhooks.md §9;
Decision P06-T06c).

:func:`open_org_stream` does everything that can fail before the response starts: the ``types`` check
(``INVALID_INPUT``), the org's ``hipaa_mode``, and the subscription on the process's
:class:`~va_events.stream_tail.StreamTailHub` (``SERVICE_UNAVAILABLE``). :class:`OrgStream` renders each event
the subscription delivers exactly as an org webhook endpoint receives it (the §7 envelope around the
:func:`~va_events.webhook_payloads.content_body`; without transcript text for a ``hipaa_mode`` org): ``retry:
3000`` first, then ``id: <event id>``, ``event: <type>``, ``data: <JSON>`` frames, and ``: ping`` after
:data:`HEARTBEAT_S` without a frame. There is no replay and no resume (``Last-Event-ID`` is ignored); the
stream ends when the client goes away, when the subscription overflows or loses Redis, or after
:data:`MAX_STREAM_S`. Internal event types are never sent, even for ``*``.
"""

from __future__ import annotations

import re
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable, Mapping
from dataclasses import dataclass
from typing import Final

from fastapi import Request
from pydantic import JsonValue
from sqlalchemy.exc import SQLAlchemyError

from va_api.auth.context import RequestContext
from va_api.services.call_tail import MAX_STREAM_S
from va_api.sse import HEARTBEAT_FRAME, retry_frame, sse_frame
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import within
from va_db.org_settings import OrgSettingsReader
from va_db.session import Database
from va_events import (
    INTERNAL_TYPES,
    DomainEvent,
    EventType,
    StreamTailError,
    StreamTailHub,
    SubscriptionClosedError,
    TailSubscription,
    envelope_body,
    strip_phi,
)
from va_events.webhook_payloads import BatchFacts, content_body
from va_events.webhook_router import batch_facts, call_metadata
from va_events.webhook_routing import matches

__all__ = [
    "HEARTBEAT_S",
    "MAX_PATTERNS",
    "MAX_STREAM_S",
    "METADATA_CACHE_SIZE",
    "RETRY_MS",
    "TYPES_MESSAGE",
    "MetadataCache",
    "OrgStream",
    "OrgStreamDeps",
    "open_org_stream",
    "org_stream_deps_of",
    "parse_types",
]

HEARTBEAT_S: Final = 15.0
RETRY_MS: Final = 3_000
MAX_PATTERNS: Final = 20
METADATA_CACHE_SIZE: Final = 1_024
TYPES_FIELD: Final = "types"
TYPES_MESSAGE: Final = "types must be a comma-separated list of event type patterns such as call.*"
UNAVAILABLE_MESSAGE: Final = "The live event stream is temporarily unavailable; retry shortly."
_PATTERN: Final = re.compile(r"[a-z0-9_.*]{1,64}")
_BATCH_PREFIX: Final = "batch."

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OrgStreamDeps:
    """What an org event stream needs from the container."""

    database: Database
    stream_tail: StreamTailHub
    org_settings: OrgSettingsReader
    clock: Clock


def org_stream_deps_of(request: Request) -> OrgStreamDeps:
    """The org stream dependencies of the running app."""
    container = request.app.state.container
    return OrgStreamDeps(
        database=container.database,
        stream_tail=container.stream_tail,
        org_settings=container.org_settings,
        clock=container.clock,
    )


def parse_types(value: str | None) -> tuple[str, ...]:
    """The glob patterns of ``types`` (``*`` when absent or empty); ``INVALID_INPUT`` on field ``types``
    unless every pattern is 1-64 of ``a-z 0-9 _ . *`` and there are at most :data:`MAX_PATTERNS`."""
    patterns = tuple(part.strip() for part in (value or "").split(",") if part.strip())
    if not patterns:
        return ("*",)
    if len(patterns) > MAX_PATTERNS or not all(_PATTERN.fullmatch(pattern) for pattern in patterns):
        raise AppError("INVALID_INPUT", TYPES_MESSAGE, field=TYPES_FIELD)
    return patterns


def _wanted(patterns: tuple[str, ...]) -> Callable[[DomainEvent], bool]:
    def predicate(event: DomainEvent) -> bool:
        return event.type not in INTERNAL_TYPES and matches(event.type, patterns)

    return predicate


async def open_org_stream(deps: OrgStreamDeps, ctx: RequestContext, types: str | None) -> OrgStream:
    """Check ``types``, read the org's ``hipaa_mode``, subscribe, and return the stream (not started)."""
    patterns = parse_types(types)
    settings = await deps.org_settings.get(ctx.org_id)
    hipaa = settings is not None and settings.hipaa_mode
    try:
        subscription = await deps.stream_tail.subscribe(ctx.org_id, _wanted(patterns))
    except StreamTailError as exc:
        raise AppError("SERVICE_UNAVAILABLE", UNAVAILABLE_MESSAGE) from exc
    return OrgStream(deps, ctx.org_id, subscription, hipaa=hipaa)


class MetadataCache:
    """The ``metadata`` of recently seen calls, least recently used first out."""

    def __init__(self, size: int = METADATA_CACHE_SIZE) -> None:
        self._size = size
        self._entries: OrderedDict[uuid.UUID, Mapping[str, JsonValue]] = OrderedDict()

    def get(self, call_id: uuid.UUID) -> Mapping[str, JsonValue] | None:
        """The cached metadata of ``call_id``, if any (marks it recently used)."""
        found = self._entries.get(call_id)
        if found is not None:
            self._entries.move_to_end(call_id)
        return found

    def put(self, call_id: uuid.UUID, metadata: Mapping[str, JsonValue]) -> None:
        """Remember ``metadata`` of ``call_id`` (dropping the least recently used beyond the size)."""
        self._entries[call_id] = metadata
        self._entries.move_to_end(call_id)
        while len(self._entries) > self._size:
            self._entries.popitem(last=False)


class OrgStream:
    """One client's stream of its org's events; iterate :meth:`frames` once, :meth:`aclose` releases it."""

    def __init__(
        self, deps: OrgStreamDeps, org_id: uuid.UUID, subscription: TailSubscription, *, hipaa: bool
    ) -> None:
        self._deps = deps
        self._clock = deps.clock
        self._org_id = org_id
        self._sub = subscription
        self._hipaa = hipaa
        self._metadata = MetadataCache()
        self._sent = 0
        self._closed = False
        self.reason = "client_gone"

    async def frames(self) -> AsyncGenerator[bytes]:
        """``retry``, then one frame per event (``: ping`` when :data:`HEARTBEAT_S` passed without one)."""
        try:
            yield retry_frame(RETRY_MS)
            started = self._clock.monotonic()
            last_sent = started
            while True:
                deadline = min(started + MAX_STREAM_S, last_sent + HEARTBEAT_S)
                try:
                    event = await self._next(deadline - self._clock.monotonic())
                except SubscriptionClosedError as closed:
                    self.reason = closed.reason
                    return
                frame = None if event is None else await self._frame(event)
                now = self._clock.monotonic()
                if frame is not None:
                    self._sent += 1
                    last_sent = now
                    yield frame
                if now >= started + MAX_STREAM_S:
                    self.reason = "max_duration"
                    return
                if now >= last_sent + HEARTBEAT_S:
                    last_sent = now
                    yield HEARTBEAT_FRAME
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Release the subscription (idempotent) and log how the stream ended."""
        if self._closed:
            return
        self._closed = True
        await self._deps.stream_tail.unsubscribe(self._sub)
        _log.info("events.org_stream_closed", org_id=str(self._org_id), reason=self.reason, frames=self._sent)

    async def _next(self, timeout_s: float) -> DomainEvent | None:
        received: list[DomainEvent] = []

        async def take() -> None:
            received.append(await self._sub.next())

        if await within(take(), self._clock, max(timeout_s, 0.0)):
            return received[0]
        return None

    async def _frame(self, event: DomainEvent) -> bytes | None:
        """The frame of ``event``; ``None`` (logged) when its call/batch facts cannot be read."""
        try:
            metadata, batch = await self._facts(event)
        except (SQLAlchemyError, OSError) as exc:
            _log.warning(
                "events.org_stream_render_failed",
                org_id=str(self._org_id),
                event_id=event.id,
                event_type=event.type.value,
                detail=type(exc).__name__,
            )
            return None
        content = content_body(event, metadata=metadata, batch=batch)
        if self._hipaa:
            content = strip_phi(event.type, content)
        return sse_frame(event.id, event.type.value, envelope_body(event, content))

    async def _facts(self, event: DomainEvent) -> tuple[Mapping[str, JsonValue] | None, BatchFacts | None]:
        """The call metadata (§5 bodies) and batch facts (§6 bodies) ``event``'s content needs."""
        metadata = None
        if event.call_id is not None and event.type is not EventType.CALL_COMPLETED:
            metadata = await self._call_metadata(event.call_id)
        batch = None
        if (
            event.batch_id is not None
            and event.call_id is None
            and event.type.value.startswith(_BATCH_PREFIX)
        ):
            batch = await self._batch(event.batch_id)
        return metadata, batch

    async def _call_metadata(self, call_id: uuid.UUID) -> Mapping[str, JsonValue]:
        """The call's metadata, read once per stream (``{}`` for an unknown call)."""
        cached = self._metadata.get(call_id)
        if cached is not None:
            return cached
        async with self._deps.database.tenant_session(self._org_id) as session:
            metadata = await call_metadata(session, self._org_id, call_id) or {}
        self._metadata.put(call_id, metadata)
        return metadata

    async def _batch(self, batch_id: uuid.UUID) -> BatchFacts | None:
        """The batch's current facts (counts change, so never cached)."""
        async with self._deps.database.tenant_session(self._org_id) as session:
            return await batch_facts(session, self._org_id, batch_id)
