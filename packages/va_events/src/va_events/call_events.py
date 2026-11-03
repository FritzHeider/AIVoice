"""``call_events`` rows of call events (specs/19-events-and-webhooks.md §1 step 4; specs/03 §6).

A row is derived from a published call event (``seq`` assigned). The row's category is the event's own
``category`` or the catalog default of its type; a call event with neither (``batch.*`` with a call
id, ...) has no row. The row id is the uuid inside ``evt_<hex>``, ``created_at`` is ``occurred_at``
(the partition key), and ``event_type`` the event's type (the SSE frame name, specs/19 §9).

Writers: :class:`DbCallEventWriter` bulk-inserts rows, one ``tenant_session`` per org (RLS applies). The
insert is idempotent (``ON CONFLICT DO NOTHING`` on the ``(created_at, id)`` key), so a retried batch never
duplicates or wedges, and an org whose transaction fails does not stop the others: the writer tries every
org and then raises :class:`CallEventWriteError` naming only the rows that were not written.
The media worker buffers rows in :class:`va_events.buffer.CallEventBuffer`; every other service hands
them to a :class:`DirectCallEventWriter`, which writes each row at once. With a ``router``,
:class:`DbCallEventWriter` also routes the rows' events (rebuilt by :func:`event_of`) to the webhook outbox
in the same per-org transaction (specs/19 §1 step 5, Decision P06-T07): a routing failure fails that org's
rows, and the retry is harmless (deliveries are ``ON CONFLICT (event_id, url) DO NOTHING``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import JsonValue
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.logging import get_logger
from va_db.models import CallEvent, CallEventCategory, CallEventLevel
from va_db.session import Database
from va_events.catalog import DEFAULT_CATEGORY, EventType
from va_events.model import DomainEvent, event_uuid

if TYPE_CHECKING:
    from va_events.publisher import WebhookRouter

__all__ = [
    "CallEventRow",
    "CallEventSink",
    "CallEventWriteError",
    "CallEventWriter",
    "DbCallEventWriter",
    "DirectCallEventWriter",
    "event_of",
    "insert_rows",
    "row_for",
]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CallEventRow:
    """One ``call_events`` row, ready to insert."""

    id: uuid.UUID
    org_id: uuid.UUID
    call_id: uuid.UUID
    seq: int
    category: CallEventCategory
    level: CallEventLevel
    message: str
    data: dict[str, JsonValue]
    created_at: datetime
    event_type: str | None = None


def row_for(event: DomainEvent) -> CallEventRow | None:
    """The row of a published call event; ``None`` for non-call events, unsequenced events, or no category."""
    if event.call_id is None or event.seq is None:
        return None
    category = event.category or DEFAULT_CATEGORY[event.type]
    if category is None:
        _log.debug("events.call_event_not_persisted", event_type=event.type.value, reason="no_category")
        return None
    return CallEventRow(
        id=event_uuid(event),
        org_id=event.org_id,
        call_id=event.call_id,
        seq=event.seq,
        category=category,
        level=event.level,
        message=event.message,
        data=dict(event.data),
        created_at=event.occurred_at,
        event_type=event.type.value,
    )


def event_of(row: CallEventRow) -> DomainEvent | None:
    """The call event a row was written from; ``None`` for rows without (a known) ``event_type``.

    The rebuilt event carries what the row stores: id ``evt_<row.id hex>``, ``occurred_at`` =
    ``created_at``, category, level, message, data, and seq (no batch, conversation, or widget id).
    """
    if row.event_type is None:
        _log.debug("events.call_event_not_routed", reason="no_event_type", call_id=str(row.call_id))
        return None
    try:
        event_type = EventType(row.event_type)
    except ValueError:
        _log.warning("events.call_event_not_routed", reason="unknown_event_type", event_type=row.event_type)
        return None
    return DomainEvent(
        id=f"evt_{row.id.hex}",
        type=event_type,
        org_id=row.org_id,
        occurred_at=row.created_at,
        call_id=row.call_id,
        category=row.category,
        level=row.level,
        message=row.message,
        data=dict(row.data),
        seq=row.seq,
    )


async def insert_rows(session: AsyncSession, rows: Sequence[CallEventRow]) -> None:
    """Insert ``rows`` in the caller's transaction; rows already stored are skipped (idempotent)."""
    if not rows:
        return
    statement = insert(CallEvent).on_conflict_do_nothing(index_elements=["created_at", "id"])
    await session.execute(statement, [asdict(row) for row in rows])


class CallEventSink(Protocol):
    """Where the publisher hands ``call_events`` rows (buffered or direct)."""

    async def add(self, row: CallEventRow) -> None: ...


class CallEventWriteError(Exception):
    """Some rows of a batch were not written; ``unwritten`` lists them in batch order (retry only those).

    The first underlying failure is chained as ``__cause__``.
    """

    def __init__(self, unwritten: Sequence[CallEventRow], *, failed_orgs: int) -> None:
        super().__init__(f"{len(unwritten)} call_events rows of {failed_orgs} org(s) were not written")
        self.unwritten: tuple[CallEventRow, ...] = tuple(unwritten)
        self.failed_orgs = failed_orgs


class CallEventWriter(Protocol):
    """Persists a batch of rows (any orgs, publish order).

    Writing a row twice must be harmless. A partial failure raises :class:`CallEventWriteError` with the rows
    that were not written; any other exception means the whole batch may be unwritten.
    """

    async def write(self, rows: Sequence[CallEventRow]) -> None: ...


class DbCallEventWriter:
    """Bulk-inserts rows into ``call_events``, one tenant transaction per org (orgs in first-seen order).

    Already stored rows are skipped (``ON CONFLICT DO NOTHING``). Every org is attempted; if any org's
    transaction fails, :class:`CallEventWriteError` carries that org's rows (a COMMIT whose reply was lost is
    reported as unwritten too, and the idempotent retry makes that harmless). With a ``router`` the rows'
    events are routed to the webhook outbox in the same transaction.
    """

    def __init__(self, database: Database, *, router: WebhookRouter | None = None) -> None:
        self._database = database
        self._router = router

    async def write(self, rows: Sequence[CallEventRow]) -> None:
        by_org: dict[uuid.UUID, list[CallEventRow]] = {}
        for row in rows:
            by_org.setdefault(row.org_id, []).append(row)
        failed: set[uuid.UUID] = set()
        first_error: Exception | None = None
        for org_id, org_rows in by_org.items():
            try:
                await self._insert(org_id, org_rows)
            except Exception as exc:  # the remaining orgs must still be written; reported below
                failed.add(org_id)
                first_error = first_error or exc
                _log.warning(
                    "events.call_events_org_write_failed",
                    org_id=str(org_id),
                    rows=len(org_rows),
                    error=type(exc).__name__,
                )
        if failed:
            unwritten = [row for row in rows if row.org_id in failed]
            raise CallEventWriteError(unwritten, failed_orgs=len(failed)) from first_error

    async def _insert(self, org_id: uuid.UUID, rows: Sequence[CallEventRow]) -> None:
        async with self._database.tenant_session(org_id) as session:
            await insert_rows(session, rows)
            if self._router is not None:
                events = [event for row in rows if (event := event_of(row)) is not None]
                if events:
                    await self._router.route_many(events, session=session)


class DirectCallEventWriter:
    """A sink that writes every row immediately (services other than the media worker)."""

    def __init__(self, writer: CallEventWriter) -> None:
        self._writer = writer

    async def add(self, row: CallEventRow) -> None:
        await self._writer.write([row])
