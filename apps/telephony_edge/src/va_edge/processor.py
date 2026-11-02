"""Asynchronous processing of stored Telnyx events (specs/06-telephony-telnyx.md §4.1 step 7, Decision
P05-T01).

The intake answers ``200`` right after the ``telnyx_events`` insert and offers the stored event here. Events
are sharded by ``call_control_id`` (``telnyx_event_id`` when absent) over ``shards`` workers, so the events of
one call leg are processed in arrival order while different legs proceed in parallel. Each shard has a bounded
queue; a full queue refuses the event (backpressure), which then stays unprocessed in Postgres until the
:class:`va_edge.reprocessor.TelnyxEventReprocessor` claims it.

Processing = parse the stored envelope, run the handler registered for the ``event_type`` within its budget
(2 s unless ``budgets`` names the type; ``call.initiated`` gets 5 s for the inbound accept), and mark the row
processed (``process_error`` on failure; a handler raising :class:`EventSettled`
records its reason there without a retry). A type without a handler is marked
processed; a type ``va_telnyx`` does not model counts as ``unhandled``. Handlers must be idempotent: an
event may be processed again after a crash, a lost mark, or a reclaim.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
import zlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_db.models import TelnyxEvent
from va_db.repositories import TelnyxEventRepo
from va_db.session import Database
from va_telnyx.webhooks import EventPayload, UnknownEventPayload, WebhookEnvelope, parse_payload

__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "DEFAULT_SHARDS",
    "HANDLER_BUDGET_S",
    "MAX_ATTEMPTS",
    "MAX_ERROR_CHARS",
    "EventHandler",
    "EventProcessor",
    "EventSettled",
    "HandlerTimeoutError",
    "ProcessResult",
    "StoredEvent",
]

DEFAULT_SHARDS: Final = 8
DEFAULT_QUEUE_SIZE: Final = 256
HANDLER_BUDGET_S: Final = 2.0
MAX_ATTEMPTS: Final = 5
MAX_ERROR_CHARS: Final = 2000

WEBHOOKS_TOTAL: Final = "va_telnyx_webhooks_total"
PROCESSING_MS: Final = "va_telnyx_webhook_processing_ms"

type ProcessResult = Literal["processed", "unhandled", "error"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """A ``telnyx_events`` row detached from its session; ``envelope`` is the verified webhook body."""

    id: uuid.UUID
    telnyx_event_id: str
    event_type: str
    occurred_at: datetime
    envelope: dict[str, Any]
    org_id: uuid.UUID | None
    call_id: uuid.UUID | None
    call_control_id: str | None
    attempts: int

    @classmethod
    def from_row(cls, row: TelnyxEvent) -> StoredEvent:
        return cls(
            id=row.id,
            telnyx_event_id=row.telnyx_event_id,
            event_type=row.event_type,
            occurred_at=row.occurred_at,
            envelope=row.payload,
            org_id=row.org_id,
            call_id=row.call_id,
            call_control_id=row.call_control_id,
            attempts=row.process_attempts,
        )

    @property
    def shard_key(self) -> str:
        return self.call_control_id or self.telnyx_event_id


type EventHandler = Callable[[StoredEvent, EventPayload], Awaitable[None]]


class EventSettled(Exception):  # noqa: N818 - an outcome, not an error
    """Raised by a handler whose event is finished for good although a reason must be recorded (an inbound
    refusal, specs/06 §7 Decision P05-T12b): the row stores ``reason`` in ``process_error``, counts as
    ``processed``, and is never retried."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HandlerTimeoutError(Exception):
    """A handler did not finish within the processing budget (it was cancelled)."""


class EventProcessor:
    """Sharded in-process queue of stored events with bounded per-shard queues."""

    def __init__(  # noqa: PLR0913 - keyword-only knobs and seams
        self,
        database: Database,
        *,
        clock: Clock,
        metrics: ServiceMetrics,
        handlers: Mapping[str, EventHandler],
        shards: int = DEFAULT_SHARDS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        budget_s: float = HANDLER_BUDGET_S,
        budgets: Mapping[str, float] | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if shards < 1 or queue_size < 1:
            msg = "shards and queue_size must be at least 1"
            raise ValueError(msg)
        self._database = database
        self._clock = clock
        self._handlers = dict(handlers)
        self._budget_s = budget_s
        self._budgets = dict(budgets or {})
        self._max_attempts = max_attempts
        self._queues: tuple[asyncio.Queue[StoredEvent], ...] = tuple(
            asyncio.Queue(maxsize=queue_size) for _ in range(shards)
        )
        self._queued: set[uuid.UUID] = set()
        self._closed = False
        self._total = metrics.counter(WEBHOOKS_TOTAL)
        self._latency = metrics.histogram(PROCESSING_MS)

    @property
    def closed(self) -> bool:
        """True once the process is shutting down: the intake answers ``503`` so Telnyx retries elsewhere."""
        return self._closed

    def close(self) -> None:
        self._closed = True

    def offer(self, event: StoredEvent) -> bool:
        """Queue ``event`` on its shard; ``False`` when the shard is full (the event waits for a reclaim).

        An event that is already queued or in progress is not queued twice (a reclaim racing the first try).
        """
        if event.id in self._queued:
            return True
        queue = self._queues[zlib.crc32(event.shard_key.encode()) % len(self._queues)]
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning(
                "telnyx.event_deferred", event_type=event.event_type, telnyx_event_id=event.telnyx_event_id
            )
            return False
        self._queued.add(event.id)
        return True

    async def run(self) -> None:
        """Run one worker per shard until cancelled."""
        async with asyncio.TaskGroup() as group:
            for index, queue in enumerate(self._queues):
                group.create_task(self._work(queue), name=f"telnyx-events-{index}")

    async def join(self) -> None:
        """Wait until every queued event has been processed."""
        for queue in self._queues:
            await queue.join()

    async def _work(self, queue: asyncio.Queue[StoredEvent]) -> None:
        while True:
            event = await queue.get()
            try:
                await self._process(event)
            finally:
                self._queued.discard(event.id)
                queue.task_done()

    async def _process(self, event: StoredEvent) -> None:
        started = self._clock.monotonic()
        result, error, settled = await self._handle(event)
        try:
            async with self._database.system_session() as session:
                await TelnyxEventRepo(session).mark_processed(
                    event.id,
                    now=self._clock.now(),
                    error=error,
                    settled_attempts=self._max_attempts if settled else None,
                )
        except Exception as exc:  # the shard must survive a database failure; the row is reclaimed later
            _log.error(
                "telnyx.event_mark_failed",
                event_type=event.event_type,
                telnyx_event_id=event.telnyx_event_id,
                error_type=type(exc).__name__,
            )
            return
        self._total.labels(event_type=event.event_type, result=result).inc()
        self._latency.labels(event_type=event.event_type).observe((self._clock.monotonic() - started) * 1000)

    async def _handle(self, event: StoredEvent) -> tuple[ProcessResult, str | None, bool]:
        """(result, ``process_error``, whether the event is settled despite the error)."""
        try:
            payload = parse_payload(WebhookEnvelope.model_validate(event.envelope).data)
            handler = self._handlers.get(event.event_type)
            if handler is not None:
                budget = self._budgets.get(event.event_type, self._budget_s)
                await self._within_budget(handler(event, payload), budget)
        except EventSettled as settled:
            return "processed", settled.reason[:MAX_ERROR_CHARS], True
        except Exception as exc:  # any handler failure is stored on the row and retried by the reprocessor
            attempt = event.attempts + 1
            _log.error(
                "telnyx.event_failed",
                event_type=event.event_type,
                telnyx_event_id=event.telnyx_event_id,
                attempt=attempt,
                abandoned=attempt >= self._max_attempts,
                error_type=type(exc).__name__,
            )
            return "error", f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS], False
        return ("unhandled" if isinstance(payload, UnknownEventPayload) else "processed"), None, False

    async def _within_budget(self, work: Awaitable[None], budget_s: float) -> None:
        task = asyncio.ensure_future(work)
        timer = asyncio.ensure_future(self._clock.sleep(budget_s))
        try:
            await asyncio.wait((task, timer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if task.cancelled():
            msg = f"the handler did not finish within {budget_s} s"
            raise HandlerTimeoutError(msg)
        task.result()
