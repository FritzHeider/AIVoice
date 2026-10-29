"""call_events partition maintenance on real Postgres (specs/03 §10 Decision, specs/28 §8.1).

Scratch tests run on the ``scratch_session`` connection (login role, rolled back): ``create_all`` makes
only the ``call_events`` parent and its partitioned indexes, so every partition seen there was created
by the helper under test. The migrated-database tests prove the helper agrees with ``0001_initial`` and
that only the owner can run it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from service_helpers import settings_of
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from va_common.settings import DatabaseSettings
from va_common.time import FrozenClock
from va_db.engine import DbRole, create_engine
from va_db.partitions import (
    drop_call_event_partitions_before,
    ensure_call_event_partitions,
    existing_call_event_partitions,
)

pytestmark = pytest.mark.integration

NOVEMBER_15: Final = datetime(2026, 11, 15, 12, tzinfo=UTC)
FIRST_FOUR: Final = [
    "call_events_p202611",
    "call_events_p202612",
    "call_events_p202701",
    "call_events_p202702",
]
BOUNDS_SQL: Final = """
SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
WHERE i.inhparent = 'call_events'::regclass ORDER BY 1
"""
INSERT_CALL_EVENT: Final = """
INSERT INTO call_events (id, org_id, call_id, seq, category, message, created_at)
VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 1, 'call', 'routed', :at)
RETURNING tableoid::regclass::text
"""
ATTACHED_INDEXES_SQL: Final = "SELECT count(*) FROM pg_inherits WHERE inhparent = CAST(:index AS regclass)"


@pytest.fixture
async def conn(scratch_session: AsyncSession) -> AsyncConnection:
    return await scratch_session.connection()


async def _landing_partition(conn: AsyncConnection, at: datetime) -> str:
    return str((await conn.execute(text(INSERT_CALL_EVENT), {"at": at})).scalar_one())


async def test_ensure_creates_the_current_month_and_three_more_once(conn: AsyncConnection) -> None:
    assert await ensure_call_event_partitions(conn, NOVEMBER_15) == FIRST_FOUR
    assert await ensure_call_event_partitions(conn, NOVEMBER_15) == []
    await conn.execute(text("SET LOCAL TimeZone = 'UTC'"))
    bounds = (await conn.execute(text(BOUNDS_SQL))).all()
    assert [tuple(row) for row in bounds] == [
        ("call_events_p202611", "FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00')"),
        ("call_events_p202612", "FOR VALUES FROM ('2026-12-01 00:00:00+00') TO ('2027-01-01 00:00:00+00')"),
        ("call_events_p202701", "FOR VALUES FROM ('2027-01-01 00:00:00+00') TO ('2027-02-01 00:00:00+00')"),
        ("call_events_p202702", "FOR VALUES FROM ('2027-02-01 00:00:00+00') TO ('2027-03-01 00:00:00+00')"),
    ]


async def test_advancing_a_month_creates_only_the_new_month(conn: AsyncConnection) -> None:
    clock = FrozenClock(NOVEMBER_15)
    await ensure_call_event_partitions(conn, clock.now())
    clock.advance(timedelta(days=31))
    assert await ensure_call_event_partitions(conn, clock.now()) == ["call_events_p202703"]
    assert await existing_call_event_partitions(conn) == (*FIRST_FOUR, "call_events_p202703")


async def test_rows_land_in_their_utc_month_and_partitions_inherit_the_indexes(conn: AsyncConnection) -> None:
    await ensure_call_event_partitions(conn, NOVEMBER_15)
    assert await _landing_partition(conn, datetime(2026, 12, 1, tzinfo=UTC)) == "call_events_p202612"
    last_november_instant = datetime(2026, 12, 1, tzinfo=UTC) - timedelta(microseconds=1)
    assert await _landing_partition(conn, last_november_instant) == "call_events_p202611"
    for index in ("call_events_org_idx", "call_events_call_idx"):
        attached = await conn.execute(text(ATTACHED_INDEXES_SQL), {"index": index})
        assert attached.scalar_one() == len(FIRST_FOUR)


@pytest.fixture
async def legacy_conn(conn: AsyncConnection) -> AsyncConnection:
    """The scratch connection holding the four November 2026 partitions plus an unrelated old partition."""
    await ensure_call_event_partitions(conn, NOVEMBER_15)
    await conn.execute(
        text(
            "CREATE TABLE call_events_legacy PARTITION OF call_events "
            "FOR VALUES FROM ('2020-01-01 00:00:00+00') TO ('2020-02-01 00:00:00+00')"
        )
    )
    return conn


async def test_drop_removes_only_expired_partitions_of_the_naming_rule(legacy_conn: AsyncConnection) -> None:
    conn = legacy_conn
    january = datetime(2027, 1, 1, tzinfo=UTC)
    assert await drop_call_event_partitions_before(conn, january) == FIRST_FOUR[:2]
    assert await drop_call_event_partitions_before(conn, january) == []
    assert await drop_call_event_partitions_before(conn, datetime(2027, 1, 15, tzinfo=UTC)) == []
    assert await existing_call_event_partitions(conn) == ("call_events_legacy", *FIRST_FOUR[2:])


async def test_ensure_after_drop_creates_nothing_before_the_current_month(
    legacy_conn: AsyncConnection,
) -> None:
    conn = legacy_conn
    await drop_call_event_partitions_before(conn, datetime(2027, 1, 1, tzinfo=UTC))
    assert await ensure_call_event_partitions(conn, datetime(2027, 1, 10, tzinfo=UTC)) == [
        "call_events_p202703",
        "call_events_p202704",
    ]


@pytest.fixture
async def owner_conn(migrated_url: str) -> AsyncIterator[AsyncConnection]:
    """A rolled-back transaction of the login role that ran the migrations and owns ``call_events``."""
    engine = create_engine(
        settings_of(DatabaseSettings, database_url=migrated_url, database_pool_size=1), None
    )
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            yield connection
            await transaction.rollback()
    finally:
        await engine.dispose()


async def _database_now(conn: AsyncConnection) -> datetime:
    moment = (await conn.execute(text("SELECT now()"))).scalar_one()
    assert isinstance(moment, datetime)
    return moment


async def test_helper_matches_the_partitions_of_the_initial_migration(owner_conn: AsyncConnection) -> None:
    assert await ensure_call_event_partitions(owner_conn, await _database_now(owner_conn)) == []


@pytest.mark.parametrize(
    ("maintain", "error"),
    [
        (
            lambda conn, now: ensure_call_event_partitions(conn, now, months_ahead=4),
            "permission denied for schema",
        ),
        (
            lambda conn, now: drop_call_event_partitions_before(conn, now + timedelta(days=400)),
            "must be owner",
        ),
    ],
    ids=["create", "drop"],
)
async def test_va_system_cannot_maintain_partitions(
    migrated_url: str, maintain: Callable[[AsyncConnection, datetime], Awaitable[list[str]]], error: str
) -> None:
    settings = settings_of(DatabaseSettings, database_url=migrated_url, database_pool_size=1)
    engine = create_engine(settings, DbRole.SYSTEM)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            now = await _database_now(connection)
            with pytest.raises(DBAPIError, match=error):
                await maintain(connection, now)
            await transaction.rollback()
    finally:
        await engine.dispose()
