"""``DbCallEventWriter`` and the buffer end to end over a migrated Postgres (one tenant session per org)."""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from call_rows import seed_call
from clock_helpers import ManualClock
from event_helpers import publisher_rig, row
from migration_helpers import query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenant_rows import OrgFactory

from va_common.time import SystemClock
from va_db.models import CallEvent, WebhookDelivery
from va_db.session import Database
from va_events import (
    CallEventBuffer,
    CallEventRow,
    CallEventWriteError,
    DbCallEventWriter,
    DbWebhookRouter,
    DomainEvent,
    EventType,
    event_uuid,
    insert_rows,
    live_body,
    new_event,
    row_body,
    row_for,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]


async def _rows(database: Database, org_id: uuid.UUID) -> list[CallEvent]:
    async with database.tenant_session(org_id) as session:
        result = await session.execute(select(CallEvent).order_by(CallEvent.call_id, CallEvent.seq))
        return list(result.scalars())


async def test_published_call_events_are_stored_per_org(database: Database) -> None:
    org_a, org_b = await OrgFactory(database).create(), await OrgFactory(database).create()
    buffer = CallEventBuffer(DbCallEventWriter(database), clock=ManualClock())
    rig = publisher_rig(buffer=buffer)
    clock = SystemClock()  # the migration created partitions around the real current month
    call_a, call_b = uuid.uuid4(), uuid.uuid4()

    published = [
        await rig.publisher.publish(
            new_event(
                EventType.CALL_LATENCY,
                org_id=org_a,
                clock=clock,
                call_id=call_a,
                message="E2E: 742ms",
                data={"e2e_ms": 742},
            )
        )
        for _ in range(3)
    ]
    other = await rig.publisher.publish(
        new_event(EventType.CALL_ERROR, org_id=org_b, clock=clock, call_id=call_b, level="error")
    )
    assert await buffer.flush_call(call_a) == 4

    rows_a = await _rows(database, org_a)
    assert [(r.id, r.call_id, r.seq) for r in rows_a] == [(event_uuid(e), call_a, e.seq) for e in published]
    assert {(r.category, r.level, r.message) for r in rows_a} == {("latency", "info", "E2E: 742ms")}
    assert rows_a[0].data == {"e2e_ms": 742}
    assert [r.created_at for r in rows_a] == [e.occurred_at for e in published]
    assert {r.event_type for r in rows_a} == {"call.latency"}
    assert [row_body(r, {"crm": 7}) for r in rows_a] == [live_body(e, {"crm": 7}) for e in published]

    rows_b = await _rows(database, org_b)
    assert [(r.id, r.category, r.level, r.seq) for r in rows_b] == [(event_uuid(other), "error", "error", 1)]


def _rows_of(
    org_id: uuid.UUID, call_id: uuid.UUID, count: int, *, at: datetime | None = None
) -> list[CallEventRow]:
    created_at = at or SystemClock().now()
    return [
        dataclasses.replace(row(call_id, seq, org_id=org_id), created_at=created_at)
        for seq in range(1, count + 1)
    ]


async def test_writing_the_same_rows_twice_stores_them_once(database: Database) -> None:
    org = await OrgFactory(database).create()
    call_id = uuid.uuid4()
    rows = _rows_of(org, call_id, 3)
    writer = DbCallEventWriter(database)
    await writer.write(rows)
    await writer.write([*rows, *_rows_of(org, uuid.uuid4(), 1)])
    stored = await _rows(database, org)
    assert sorted(r.id for r in stored if r.call_id == call_id) == sorted(r.id for r in rows)
    assert len(stored) == 4


async def test_one_failing_org_leaves_the_others_written_once(migrated_url: str, database: Database) -> None:
    org_a, org_b = await OrgFactory(database).create(), await OrgFactory(database).create()
    call_a, call_b = uuid.uuid4(), uuid.uuid4()
    # no partition holds January 2000, so org B's transaction fails until the test creates one
    unpartitioned = datetime(2000, 1, 15, tzinfo=UTC)
    rows_a, rows_b = _rows_of(org_a, call_a, 2), _rows_of(org_b, call_b, 2, at=unpartitioned)
    buffer = CallEventBuffer(DbCallEventWriter(database), clock=ManualClock())
    for row_a, row_b in zip(rows_a, rows_b, strict=True):
        await buffer.add(row_b)
        await buffer.add(row_a)

    with pytest.raises(CallEventWriteError) as raised:
        await buffer.flush()
    assert raised.value.unwritten == tuple(rows_b)
    assert raised.value.failed_orgs == 1
    assert raised.value.__cause__ is not None
    assert [r.id for r in await _rows(database, org_a)] == [r.id for r in rows_a]
    assert await _rows(database, org_b) == []
    assert buffer.pending == 2

    await asyncio.to_thread(
        query,
        migrated_url,
        "CREATE TABLE call_events_p200001 PARTITION OF call_events "
        "FOR VALUES FROM ('2000-01-01 00:00:00+00') TO ('2000-02-01 00:00:00+00')",
    )
    await asyncio.to_thread(
        query,
        migrated_url,
        "GRANT SELECT, INSERT, UPDATE, DELETE ON call_events_p200001 TO va_app, va_system",
    )
    assert await buffer.flush() == 2
    assert [r.id for r in await _rows(database, org_b)] == [r.id for r in rows_b]
    assert [r.id for r in await _rows(database, org_a)] == [r.id for r in rows_a]


async def test_insert_rows_writes_in_the_callers_transaction_idempotently(database: Database) -> None:
    org = await OrgFactory(database).create()
    call_id = uuid.uuid4()
    rows = _rows_of(org, call_id, 2)
    async with database.tenant_session(org) as session:
        await insert_rows(session, rows)
        await insert_rows(session, rows)
        await insert_rows(session, [])
    assert sorted(r.seq for r in await _rows(database, org)) == [1, 2]


async def test_insert_rows_rolls_back_with_the_callers_transaction(database: Database) -> None:
    org = await OrgFactory(database).create()

    class AbortError(Exception):
        """Raised inside the transaction to roll it back."""

    with pytest.raises(AbortError):
        async with database.tenant_session(org) as session:
            await insert_rows(session, _rows_of(org, uuid.uuid4(), 1))
            raise AbortError
    assert await _rows(database, org) == []


async def _delivered_types(database: Database, org_id: uuid.UUID) -> list[str]:
    async with database.tenant_session(org_id) as session:
        rows = await session.execute(
            select(WebhookDelivery.event_type, WebhookDelivery.format)
            .where(WebhookDelivery.org_id == org_id)
            .order_by(WebhookDelivery.event_type)
        )
        return [f"{event_type}/{fmt}" for event_type, fmt in rows]


def _call_event(event_type: EventType, org_id: uuid.UUID, call_id: uuid.UUID, seq: int) -> CallEventRow:
    published = new_event(
        event_type, org_id=org_id, clock=SystemClock(), call_id=call_id, message="E2E: 700ms"
    ).model_copy(update={"seq": seq})
    stored = row_for(published)
    assert stored is not None
    return stored


async def test_a_routing_writer_puts_the_rows_events_in_the_webhook_outbox(database: Database) -> None:
    url = f"https://call-{secrets.token_hex(4)}.hooks.test/in"
    call = await seed_call(database, config={"webhook": url, "webhook_events": ["latency"]})
    rows = [
        _call_event(EventType.CALL_LATENCY, call.org_id, call.call_id, 1),
        _call_event(EventType.CALL_TURN, call.org_id, call.call_id, 2),
        dataclasses.replace(row(call.call_id, 3, org_id=call.org_id), created_at=SystemClock().now()),
    ]
    writer = DbCallEventWriter(database, router=DbWebhookRouter(database))

    await writer.write(rows)
    await writer.write(rows)

    assert len(await _rows(database, call.org_id)) == 3
    assert await _delivered_types(database, call.org_id) == ["call.latency/bland"]


class _RouterDownError(RuntimeError):
    """The failing router's error."""


class _FailingRouter:
    """Routes one org's events and fails for another."""

    def __init__(self, inner: DbWebhookRouter, failing_org: uuid.UUID) -> None:
        self._inner = inner
        self._failing_org = failing_org

    async def route(self, event: DomainEvent, *, session: AsyncSession | None) -> None:
        await self.route_many([event], session=session)

    async def route_many(self, events: Sequence[DomainEvent], *, session: AsyncSession | None) -> None:
        if any(event.org_id == self._failing_org for event in events):
            raise _RouterDownError
        await self._inner.route_many(events, session=session)


async def test_a_routing_failure_leaves_neither_row_nor_delivery_of_that_org(database: Database) -> None:
    url = f"https://call-{secrets.token_hex(4)}.hooks.test/in"
    good = await seed_call(database, config={"webhook": url, "webhook_events": ["latency"]})
    bad = await seed_call(database, config={"webhook": url, "webhook_events": ["latency"]})
    bad_row = _call_event(EventType.CALL_LATENCY, bad.org_id, bad.call_id, 1)
    good_row = _call_event(EventType.CALL_LATENCY, good.org_id, good.call_id, 1)
    writer = DbCallEventWriter(database, router=_FailingRouter(DbWebhookRouter(database), bad.org_id))

    with pytest.raises(CallEventWriteError) as raised:
        await writer.write([bad_row, good_row])

    assert raised.value.unwritten == (bad_row,)
    assert await _rows(database, bad.org_id) == []
    assert await _delivered_types(database, bad.org_id) == []
    assert [r.id for r in await _rows(database, good.org_id)] == [good_row.id]
    assert await _delivered_types(database, good.org_id) == ["call.latency/bland"]
