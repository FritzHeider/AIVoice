"""``DbCallEventWriter`` and the buffer end to end over a migrated Postgres (one tenant session per org)."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from clock_helpers import ManualClock
from event_helpers import publisher_rig, row
from migration_helpers import migrated_postgres, query
from sqlalchemy import select
from tenant_rows import OrgFactory, open_database

from va_common.time import SystemClock
from va_db.models import CallEvent
from va_db.session import Database
from va_events import (
    CallEventBuffer,
    CallEventRow,
    CallEventWriteError,
    DbCallEventWriter,
    EventType,
    event_uuid,
    insert_rows,
    new_event,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(240)]


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


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
