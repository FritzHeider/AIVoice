"""Callback children of the ``schedule_callback`` tool on a migrated database (specs/13 §6 Decisions
P08-T02b2, P08-FIX-13)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from call_rows import SeededCall, seed_call
from sqlalchemy import func, select
from tenant_rows import OrgFactory, open_database

from va_common.ids import new_id
from va_db.call_children import COPIED_COLUMNS
from va_db.callbacks import CallbackLimitError, ScheduledCallback, schedule_callback
from va_db.models import Call
from va_db.repositories import CallRepo, NotFoundError
from va_db.session import Database
from va_domain.call_workflow import call_workflow_id

pytestmark = pytest.mark.integration

WHEN = datetime(2026, 11, 6, 17, 30, tzinfo=UTC)
CONFIG = {"task": "Sell", "timezone": "America/Los_Angeles", "retry_attempt": 1}


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def _schedule(
    database: Database, parent: SeededCall, child_id: uuid.UUID, *, note: str | None = "Blue plan"
) -> ScheduledCallback:
    async with database.tenant_session(parent.org_id) as session:
        return await schedule_callback(
            session, parent.org_id, parent.call_id, child_id=child_id, scheduled_for=WHEN, note=note
        )


async def _children(database: Database, parent: SeededCall) -> list[Call]:
    async with database.tenant_session(parent.org_id) as session:
        rows = await session.scalars(select(Call).where(Call.parent_call_id == parent.call_id))
        return list(rows)


async def test_an_outbound_parent_gets_a_scheduled_child_with_its_columns(database: Database) -> None:
    parent = await seed_call(
        database,
        status="in_progress",
        config=CONFIG,
        request_data={"name": "Ana"},
        metadata_={"crm": "7"},
        external_id="ext-1",
    )
    child_id = new_id()
    scheduled = await _schedule(database, parent, child_id)

    assert scheduled == ScheduledCallback(child_id, WHEN)
    [child] = await _children(database, parent)
    async with database.tenant_session(parent.org_id) as session:
        stored = await session.get_one(Call, parent.call_id)
        assert {name: getattr(child, name) for name in COPIED_COLUMNS} == {
            name: getattr(stored, name) for name in COPIED_COLUMNS
        }
    assert child.id == child_id
    assert (child.status, child.queue_status) == ("scheduled", "new")
    assert child.start_time == WHEN
    assert child.credit_hold_id is None
    assert child.workflow_id == call_workflow_id(child_id)
    assert child.config == {
        "task": "Sell",
        "timezone": "America/Los_Angeles",
        "start_time": "2026-11-06T17:30:00Z",
        "callback_note": "Blue plan",
    }


async def test_an_inbound_parent_calls_the_caller_back_from_the_called_number(database: Database) -> None:
    parent = await seed_call(
        database,
        status="in_progress",
        direction="inbound",
        to_number="+14155550123",
        from_number="+14155550777",
        config=CONFIG,
    )
    await _schedule(database, parent, new_id(), note=None)

    [child] = await _children(database, parent)
    assert (child.direction, child.to_number, child.from_number) == (
        "outbound",
        "+14155550777",
        "+14155550123",
    )
    assert "callback_note" not in child.config


async def test_a_second_pending_callback_is_refused(database: Database) -> None:
    parent = await seed_call(database, status="in_progress", config=CONFIG)
    first = new_id()
    await _schedule(database, parent, first)
    with pytest.raises(CallbackLimitError):
        await _schedule(database, parent, new_id())

    assert [child.id for child in await _children(database, parent)] == [first]


async def test_a_callback_that_left_scheduled_frees_the_slot(database: Database) -> None:
    parent = await seed_call(database, status="in_progress", config=CONFIG)
    first = new_id()
    await _schedule(database, parent, first)
    async with database.tenant_session(parent.org_id) as session:
        await CallRepo(session, parent.org_id).transition(first, "canceled", from_={"scheduled"})
    second = new_id()
    await _schedule(database, parent, second)

    async with database.tenant_session(parent.org_id) as session:
        count = await session.scalar(
            select(func.count()).select_from(Call).where(Call.parent_call_id == parent.call_id)
        )
    assert count == 2


async def test_a_call_of_another_org_is_not_found(database: Database) -> None:
    parent = await seed_call(database, status="in_progress", config=CONFIG)
    other = await OrgFactory(database).create()
    with pytest.raises(NotFoundError):
        async with database.tenant_session(other) as session:
            await schedule_callback(
                session, other, parent.call_id, child_id=new_id(), scheduled_for=WHEN, note=None
            )
    assert await _children(database, parent) == []
