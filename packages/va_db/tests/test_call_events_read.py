"""``va_db.call_events_read.load_call_events`` and ``va_db.call_object.load_transcripts`` on a migrated
database (specs/05-calls-api.md §7.6, §7.9; Decision P05-T16b)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from call_rows import SeededCall, seed_call
from sqlalchemy import select
from tenant_rows import open_database

from va_common.ids import new_id
from va_db.call_events_read import load_call_events
from va_db.call_object import load_call_objects, load_transcripts
from va_db.models import Call, CallEvent, CallTurn, CallTurnRole
from va_db.session import Database

pytestmark = pytest.mark.integration

CATEGORIES = ("call", "latency", "tool", "call", "route", "call", "tool")


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def _events(database: Database, call: SeededCall, categories: Sequence[str]) -> None:
    async with database.tenant_session(call.org_id) as session:
        session.add_all(
            CallEvent(
                id=new_id(),
                org_id=call.org_id,
                call_id=call.call_id,
                seq=seq,
                category=category,
                message=f"event {seq}",
                data={"seq": seq},
            )
            for seq, category in enumerate(categories, start=1)
        )


async def _page(
    database: Database, call: SeededCall, *, org_id: uuid.UUID | None = None, **query: Any
) -> tuple[list[int], int | None]:
    org = call.org_id if org_id is None else org_id
    options: dict[str, Any] = {"after_seq": 0, "limit": 100, "categories": None, **query}
    async with database.tenant_session(org) as session:
        rows, next_after = await load_call_events(session, org, call.call_id, **options)
    return [row.seq for row in rows], next_after


async def test_events_come_ordered_by_seq(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    await _events(database, call, list(reversed(CATEGORIES)))
    assert await _page(database, call) == ([1, 2, 3, 4, 5, 6, 7], None)


async def test_after_seq_pages_through_without_duplicates(database: Database) -> None:
    call = await seed_call(database, status="completed")
    await _events(database, call, CATEGORIES)
    seen: list[int] = []
    after: int | None = 0
    pages = 0
    while after is not None:
        seqs, after = await _page(database, call, after_seq=after, limit=3)
        seen.extend(seqs)
        pages += 1
    assert (seen, pages) == ([1, 2, 3, 4, 5, 6, 7], 3)


async def test_a_page_that_ends_exactly_at_the_last_event_has_no_next(database: Database) -> None:
    call = await seed_call(database, status="completed")
    await _events(database, call, CATEGORIES[:6])
    assert await _page(database, call, limit=3) == ([1, 2, 3], 3)
    assert await _page(database, call, after_seq=3, limit=3) == ([4, 5, 6], None)


@pytest.mark.parametrize(
    ("categories", "expected"),
    [(["tool"], [3, 7]), (["call", "route"], [1, 4, 5, 6]), (["sms"], [])],
)
async def test_category_filter(database: Database, categories: list[str], expected: list[int]) -> None:
    call = await seed_call(database, status="completed")
    await _events(database, call, CATEGORIES)
    assert await _page(database, call, categories=categories) == (expected, None)


async def test_another_orgs_events_never_appear(database: Database) -> None:
    call = await seed_call(database, status="completed")
    await _events(database, call, CATEGORIES)
    intruder = await seed_call(database, status="completed")
    assert await _page(database, call, org_id=intruder.org_id) == ([], None)


async def test_load_transcripts_matches_the_call_object(database: Database) -> None:
    call = await seed_call(database, status="completed")
    roles: list[tuple[CallTurnRole, str]] = [
        ("user", "Hello?"),
        ("system", "hidden"),
        ("assistant", "Hi there."),
        ("agent-action", "Pressed 1."),
    ]
    async with database.tenant_session(call.org_id) as session:
        session.add_all(
            CallTurn(
                id=new_id(),
                org_id=call.org_id,
                call_id=call.call_id,
                turn_index=index,
                role=role,
                text_=text,
                start_ms=index * 1000,
                end_ms=index * 1000 + 500,
            )
            for index, (role, text) in enumerate(roles)
        )
    async with database.tenant_session(call.org_id) as session:
        row = await session.scalar(select(Call).where(Call.id == call.call_id))
        assert row is not None
        transcripts = await load_transcripts(session, row)
        (detail,) = await load_call_objects(session, [row], recording_urls={})
    assert transcripts == detail.transcripts
    assert [entry.text for entry in transcripts] == ["Hello?", "Hi there.", "Pressed 1."]
