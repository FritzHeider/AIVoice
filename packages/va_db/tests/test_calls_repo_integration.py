"""``CallRepo`` / ``CallLegRepo`` guarded transitions and ``CallLegLookupRepo`` on a migrated database
(specs/05-calls-api.md §8 Decision P05-T02; specs/03 §2)."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from call_rows import load_call, load_leg, seed_call, seed_leg
from tenant_rows import open_database

from va_common.ids import new_id
from va_db.repositories import (
    CallLegLookupRepo,
    CallLegRepo,
    CallRepo,
    CallTransitionError,
    LegRef,
    NotFoundError,
)
from va_db.session import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


def _ccid() -> str:
    return f"v3:{secrets.token_hex(8)}"


async def test_transition_applies_values_and_returns_the_row(database: Database) -> None:
    call = await seed_call(database, status="ringing")
    at = datetime.now(UTC)
    async with database.tenant_session(call.org_id) as session:
        row = await CallRepo(session, call.org_id).transition(
            call.call_id, "in_progress", answered_at=at, started_at=at
        )
        assert row.status == "in_progress"
        assert row.answered_at == at
    stored = await load_call(database, call)
    assert (stored.status, stored.started_at) == ("in_progress", at)


async def test_conflict_names_the_current_status_and_changes_nothing(database: Database) -> None:
    call = await seed_call(database, status="completed")
    async with database.tenant_session(call.org_id) as session:
        repo = CallRepo(session, call.org_id)
        with pytest.raises(CallTransitionError) as caught:
            await repo.transition(call.call_id, "in_progress", answered_at=datetime.now(UTC))
    assert caught.value.code == "CONFLICT"
    assert caught.value.current == "completed"
    stored = await load_call(database, call)
    assert (stored.status, stored.answered_at) == ("completed", None)


async def test_repeating_a_transition_is_a_conflict(database: Database) -> None:
    call = await seed_call(database, status="dialing")
    async with database.tenant_session(call.org_id) as session:
        repo = CallRepo(session, call.org_id)
        await repo.transition(call.call_id, "ringing")
        with pytest.raises(CallTransitionError) as caught:
            await repo.transition(call.call_id, "ringing")
    assert caught.value.current == "ringing"


async def test_explicit_sources_narrow_the_allowed_set(database: Database) -> None:
    call = await seed_call(database, status="dialing")
    async with database.tenant_session(call.org_id) as session:
        repo = CallRepo(session, call.org_id)
        with pytest.raises(CallTransitionError):
            await repo.transition(call.call_id, "in_progress", from_=["ringing"])
        with pytest.raises(ValueError, match="in_progress cannot be entered from completed"):
            await repo.transition(call.call_id, "in_progress", from_=["ringing", "completed"])
        with pytest.raises(ValueError, match="may not set status"):
            await repo.transition(call.call_id, "ringing", status="busy")


async def test_other_org_calls_are_not_found(database: Database) -> None:
    call = await seed_call(database, status="dialing")
    other = await seed_call(database, status="dialing")
    async with database.tenant_session(other.org_id) as session:
        repo = CallRepo(session, other.org_id)
        with pytest.raises(NotFoundError):
            await repo.transition(call.call_id, "ringing")
        with pytest.raises(NotFoundError):
            await repo.update_fields(call.call_id, answered_by="human")
    assert (await load_call(database, call)).status == "dialing"


async def test_update_fields_sets_values_without_status_change(database: Database) -> None:
    call = await seed_call(database, status="in_progress")
    async with database.tenant_session(call.org_id) as session:
        repo = CallRepo(session, call.org_id)
        row = await repo.update_fields(call.call_id, answered_by="voicemail")
        assert (row.status, row.answered_by) == ("in_progress", "voicemail")
        with pytest.raises(ValueError, match="at least one value"):
            await repo.update_fields(call.call_id)


async def test_record_initiated_is_idempotent_and_fills_missing_ids(database: Database) -> None:
    call = await seed_call(database)
    ccid = _ccid()
    async with database.tenant_session(call.org_id) as session:
        legs = CallLegRepo(session, call.org_id)
        first = await legs.record_initiated(call.call_id, "primary", ccid, call_leg_id="leg-1")
        again = await legs.record_initiated(
            call.call_id, "primary", ccid, call_leg_id="leg-2", call_session_id="sess-1"
        )
        assert again.id == first.id
    leg = await load_leg(database, call, ccid)
    assert leg is not None
    assert (leg.call_leg_id, leg.call_session_id, leg.state) == ("leg-1", "sess-1", "initiated")


async def test_primary_is_the_first_primary_leg_of_the_call(database: Database) -> None:
    call = await seed_call(database)
    other = await seed_call(database, org_id=call.org_id)
    first, second = _ccid(), _ccid()
    await seed_leg(database, call, _ccid(), role="transfer_target")
    await seed_leg(database, other, _ccid())
    async with database.tenant_session(call.org_id) as session:
        assert await CallLegRepo(session, call.org_id).primary(call.call_id) is None
    await seed_leg(database, call, first)
    await seed_leg(database, call, second)
    async with database.tenant_session(call.org_id) as session:
        leg = await CallLegRepo(session, call.org_id).primary(call.call_id)
    assert leg is not None
    assert leg.call_control_id == first


async def test_advance_is_guarded(database: Database) -> None:
    call = await seed_call(database)
    ccid = _ccid()
    await seed_leg(database, call, ccid)
    at = datetime.now(UTC)
    async with database.tenant_session(call.org_id) as session:
        legs = CallLegRepo(session, call.org_id)
        answered = await legs.advance(ccid, "answered", answered_at=at)
        assert answered is not None
        assert answered.state == "answered"
        assert await legs.advance(ccid, "ringing") is None
        assert await legs.advance(ccid, "hangup", hangup_cause="normal_clearing") is not None
        assert await legs.advance(ccid, "hangup") is None
        assert await legs.advance(_ccid(), "answered") is None
        assert await legs.set_amd(ccid, "machine")
        assert not await legs.set_amd(_ccid(), "machine")
        found = await legs.by_control_id(ccid)
        assert found is not None
        assert (found.state, found.amd_result, found.hangup_cause) == ("hangup", "machine", "normal_clearing")


async def test_lookup_resolves_any_org_and_misses_unknown(database: Database) -> None:
    call = await seed_call(database)
    ccid = _ccid()
    await seed_leg(database, call, ccid, role="transfer_target")
    async with database.system_session() as session:
        repo = CallLegLookupRepo(session)
        assert await repo.by_call_control_id(ccid) == LegRef(call.org_id, call.call_id, "transfer_target")
        assert await repo.by_call_control_id(f"v3:{new_id().hex}") is None
