"""``record_answered_end`` on a migrated database (specs/05 §8, §9; specs/07 §18 Decision P05-T11b)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from call_rows import SeededCall, load_call, seed_call
from tenant_rows import open_database

from va_db.call_end import ended_values, record_answered_end, stored_reason
from va_db.call_states import EndReason
from va_db.repositories import CallRepo, CallTransitionError
from va_db.session import Database

pytestmark = pytest.mark.integration

ANSWERED_AT = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def _end(
    database: Database,
    seeded: SeededCall,
    reason: EndReason = "agent_end_call",
    *,
    transfer_started_at: datetime | None = None,
    error_message: str | None = None,
) -> EndReason:
    async with database.tenant_session(seeded.org_id) as session:
        call = await CallRepo(session, seeded.org_id).get(seeded.call_id)
        _, recorded = await record_answered_end(
            session,
            call,
            reason,
            ended_at=ANSWERED_AT + timedelta(seconds=90),
            transfer_started_at=transfer_started_at,
            error_message=error_message,
        )
    return recorded


async def test_an_answered_call_completes_with_its_durations(database: Database) -> None:
    seeded = await seed_call(database, status="in_progress", answered_at=ANSWERED_AT)
    assert await _end(database, seeded) == "agent_end_call"
    call = await load_call(database, seeded)
    assert (call.status, call.end_reason, call.ended_by) == ("completed", "agent_end_call", "agent")
    assert call.ended_at == ANSWERED_AT + timedelta(seconds=90)
    assert call.connected_seconds == Decimal(90)
    assert call.error_message is None


async def test_a_stored_reason_and_message_win(database: Database) -> None:
    seeded = await seed_call(
        database,
        status="in_progress",
        answered_at=ANSWERED_AT,
        end_reason="api_stop",
        error_message="first",
    )
    assert await _end(database, seeded, "error", error_message="second") == "api_stop"
    call = await load_call(database, seeded)
    assert (call.end_reason, call.ended_by, call.error_message) == ("api_stop", "api", "first")


async def test_transfer_seconds_start_at_the_transfer(database: Database) -> None:
    seeded = await seed_call(database, status="transferred", answered_at=ANSWERED_AT)
    await _end(database, seeded, transfer_started_at=ANSWERED_AT + timedelta(seconds=30))
    call = await load_call(database, seeded)
    assert call.status == "completed"
    assert call.transfer_seconds == Decimal(60)


async def test_an_unanswered_call_is_refused(database: Database) -> None:
    seeded = await seed_call(database, status="ringing")
    with pytest.raises(CallTransitionError) as caught:
        await _end(database, seeded)
    assert caught.value.current == "ringing"
    assert (await load_call(database, seeded)).status == "ringing"


def test_stored_reason_and_values() -> None:
    assert stored_reason("user_hangup") == "user_hangup"
    assert stored_reason("nope") is None
    assert stored_reason(None) is None
    assert ended_values("api_stop", ANSWERED_AT, None) == {
        "end_reason": "api_stop",
        "ended_by": "api",
        "ended_at": ANSWERED_AT,
    }
    assert ended_values("error", ANSWERED_AT, "boom")["error_message"] == "boom"
