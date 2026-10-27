"""Repositories against a migrated database (specs/03-data-model.md §1, §2; specs/31-error-catalog.md §2, §3).

Tenant tests run as ``va_app`` inside ``tenant_session`` with a fresh org each, so lists are naturally
scoped. The module database is shared by the tests of this module, so system-repository tests use time
windows no other test writes to, or clear the one table they poll (``webhook_deliveries``).
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from model_factories import make_org
from sqlalchemy import and_, delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tenant_rows import OrgFactory, RowFactory, RowKey, open_database

from va_common.errors import AppError
from va_common.ids import new_id
from va_common.time import FrozenClock
from va_db.base import Base
from va_db.models import (
    Call,
    CreditHold,
    CreditLedger,
    Disposition,
    PhoneNumber,
    Prompt,
    TelnyxEvent,
    Voice,
    WebhookDelivery,
)
from va_db.pagination import Page
from va_db.repositories import (
    DEFAULT_LEASE,
    PURGE_RULES,
    BalanceDrift,
    BillingReconcileRepo,
    NewTelnyxEvent,
    NotFoundError,
    OutboxRepo,
    PhoneNumberRoutingRepo,
    RetentionPolicy,
    RetentionRepo,
    SchedulerRepo,
    TelnyxEventRepo,
    TenantRepository,
)
from va_db.session import Database

pytestmark = pytest.mark.integration

_BASE = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
_SECOND = timedelta(seconds=1)


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


@pytest.fixture
async def org(database: Database) -> uuid.UUID:
    return await OrgFactory(database).create()


async def _insert(
    database: Database, table_name: str, org_id: uuid.UUID | None, overrides: Sequence[Mapping[str, Any]]
) -> list[RowKey]:
    """Insert one minimal row of ``table_name`` per ``overrides`` entry (system scope) and return the keys."""
    factory, table = RowFactory(), Base.metadata.tables[table_name]
    keys = []
    async with database.system_session() as session:
        for override in overrides:
            values = await factory.values(session, table, org_id) | dict(override)
            keys.append(await factory.insert_values(session, table, values))
    return keys


async def _ids(
    database: Database, table_name: str, org_id: uuid.UUID, overrides: Sequence[Mapping[str, Any]]
) -> list[uuid.UUID]:
    return [key["id"] for key in await _insert(database, table_name, org_id, overrides)]


async def _exists(database: Database, table_name: str, key: RowKey) -> bool:
    table = Base.metadata.tables[table_name]
    statement = (
        select(func.count()).select_from(table).where(and_(*(table.c[k] == v for k, v in key.items())))
    )
    async with database.system_session() as session:
        return bool(await session.scalar(statement))


def _prompts(
    session: AsyncSession, org_id: uuid.UUID, clock: FrozenClock | None = None
) -> TenantRepository[Prompt]:
    return TenantRepository(Prompt, session, org_id, clock=clock)


# ---- TenantRepository -------------------------------------------------------------------------------------


async def test_create_update_soft_delete_round_trip(database: Database, org: uuid.UUID) -> None:
    clock = FrozenClock(datetime(2026, 5, 1, 8, 30, tzinfo=UTC))
    async with database.tenant_session(org) as session:
        created = await _prompts(session, org).create({"name": "greeting", "content": "Hello"})
        assert (created.org_id, created.tags, created.deleted_at) == (org, [], None)
        assert created.created_at == created.updated_at
    async with database.tenant_session(org) as session:
        updated = await _prompts(session, org).update(created.id, {"name": "welcome"})
        assert (updated.name, updated.content) == ("welcome", "Hello")
        assert updated.updated_at > created.updated_at
    async with database.tenant_session(org) as session:
        repo = _prompts(session, org, clock)
        deleted = await repo.soft_delete(created.id)
        assert deleted.deleted_at == clock.now()
        with pytest.raises(NotFoundError):
            await repo.get(created.id)
        assert (await repo.get(created.id, include_deleted=True)).id == created.id
        assert (await repo.list()).items == ()
        assert [row.id for row in (await repo.list(include_deleted=True)).items] == [created.id]
        with pytest.raises(NotFoundError, match="prompts not found"):
            await repo.soft_delete(created.id)
        with pytest.raises(NotFoundError):
            await repo.update(created.id, {"name": "again"})


async def test_create_accepts_an_explicit_id(database: Database, org: uuid.UUID) -> None:
    row_id = new_id()
    async with database.tenant_session(org) as session:
        assert (await _prompts(session, org).create({"id": row_id, "name": "n", "content": "c"})).id == row_id


async def test_rows_of_another_org_are_not_found(database: Database, org: uuid.UUID) -> None:
    other = await OrgFactory(database).create()
    async with database.tenant_session(other) as session:
        theirs = await _prompts(session, other).create({"name": "secret", "content": "B only"})
    async with database.tenant_session(org) as session:
        repo = _prompts(session, org)
        for attempt in (
            repo.get(theirs.id),
            repo.get(theirs.id, include_deleted=True),
            repo.update(theirs.id, {"name": "stolen"}),
            repo.soft_delete(theirs.id),
        ):
            with pytest.raises(NotFoundError) as caught:
                await attempt
            assert str(theirs.id) not in caught.value.message
        assert theirs.id not in {row.id for row in (await repo.list(limit=100)).items}
    async with database.tenant_session(other) as session:
        unchanged = await _prompts(session, other).get(theirs.id)
        assert (unchanged.name, unchanged.deleted_at) == ("secret", None)


async def test_platform_rows_are_not_listed_by_the_generic_repository(
    database: Database, org: uuid.UUID
) -> None:
    (platform,) = await _insert(database, "voices", None, [{}])
    async with database.tenant_session(org) as session:
        visible = set((await session.scalars(select(Voice.id))).all())
        assert platform["id"] in visible  # RLS platform_read lets the org see it ...
        repo = TenantRepository(Voice, session, org)
        assert platform["id"] not in {
            row.id for row in (await repo.list(limit=100)).items
        }  # ... the repo does not
        with pytest.raises(NotFoundError, match="voices not found"):
            await repo.get(platform["id"])


async def test_unique_violation_is_conflict_and_the_session_stays_usable(
    database: Database, org: uuid.UUID
) -> None:
    async with database.tenant_session(org) as session:
        repo = TenantRepository(Disposition, session, org)
        booked = (await repo.create({"tag": "BOOKED"})).id
        other = (await repo.create({"tag": "CALLBACK"})).id
        with pytest.raises(AppError) as caught:
            await repo.create({"tag": "BOOKED"})
        assert (caught.value.code, caught.value.http_status, caught.value.message) == (
            "CONFLICT",
            409,
            "dispositions already exists.",
        )
        with pytest.raises(AppError, match="CONFLICT"):
            await repo.update(other, {"tag": "BOOKED"})
        with pytest.raises(IntegrityError, match="dispositions_tag_check"):
            await repo.create({"tag": "not upper case"})
        # A failed savepoint expires the rows it touched; reading them again reloads them.
        assert (await repo.get(booked)).tag == "BOOKED"
        assert (await repo.get(other)).tag == "CALLBACK"
    async with database.tenant_session(org) as session:
        assert sorted(
            row.tag for row in (await TenantRepository(Disposition, session, org).list()).items
        ) == [
            "BOOKED",
            "CALLBACK",
        ]


async def _seed_prompts(
    database: Database, org: uuid.UUID, stamps: Sequence[datetime]
) -> list[tuple[datetime, uuid.UUID]]:
    """Insert one prompt per ``created_at`` and return their ``(created_at, id)`` keys."""
    keys = [(stamp, new_id()) for stamp in stamps]
    rows = [
        {"id": row_id, "org_id": org, "name": f"p{index}", "content": "c", "created_at": stamp}
        for index, (stamp, row_id) in enumerate(keys)
    ]
    async with database.tenant_session(org) as session:
        await session.execute(insert(Prompt), rows)
    return keys


async def _page(database: Database, org: uuid.UUID, cursor: str | None, limit: int = 3) -> Page[Prompt]:
    async with database.tenant_session(org) as session:
        return await _prompts(session, org).list(limit=limit, cursor=cursor)


def _keys(page: Page[Prompt]) -> list[tuple[datetime, uuid.UUID]]:
    return [(row.created_at, row.id) for row in page.items]


async def test_pages_walk_newest_first_with_an_id_tie_break(database: Database, org: uuid.UUID) -> None:
    stamps = [_BASE + timedelta(minutes=minute) for minute in (0, 1, 2, 3, 3, 4, 5)]  # two rows share 09:03
    expected = sorted(await _seed_prompts(database, org, stamps), reverse=True)
    first = await _page(database, org, None)
    second = await _page(database, org, first.next_cursor)
    third = await _page(database, org, second.next_cursor)
    assert [len(page.items) for page in (first, second, third)] == [3, 3, 1]
    assert [page.has_more for page in (first, second, third)] == [True, True, False]
    assert first.next_cursor is not None and second.next_cursor is not None
    assert third.next_cursor is None
    assert _keys(first) + _keys(second) + _keys(third) == expected
    assert (await _page(database, org, None, limit=7)).pagination() == {
        "next_cursor": None,
        "has_more": False,
    }


async def test_pages_are_stable_across_inserts(database: Database, org: uuid.UUID) -> None:
    stamps = [_BASE + timedelta(minutes=minute) for minute in range(7)]
    original = sorted(await _seed_prompts(database, org, stamps), reverse=True)
    first = await _page(database, org, None)
    newer = await _seed_prompts(database, org, [_BASE + timedelta(days=1, minutes=m) for m in range(3)])
    (older,) = await _seed_prompts(database, org, [_BASE - timedelta(days=1)])
    second = await _page(database, org, first.next_cursor)
    third = await _page(database, org, second.next_cursor)
    walked = _keys(first) + _keys(second) + _keys(third)
    assert walked == [*original, older]
    assert len(set(walked)) == len(walked)
    assert not set(newer) & set(walked)
    assert (third.has_more, third.next_cursor) == (False, None)


async def test_list_filters_and_validates(database: Database, org: uuid.UUID) -> None:
    keys = await _seed_prompts(database, org, [_BASE, _BASE + _SECOND])
    async with database.tenant_session(org) as session:
        repo = _prompts(session, org)
        assert [row.id for row in (await repo.list(where=[Prompt.name == "p1"])).items] == [keys[1][1]]
        for bad in ({"limit": 0}, {"limit": 101}, {"cursor": "garbage!"}):
            with pytest.raises(AppError, match="INVALID_INPUT"):
                await repo.list(**bad)


# ---- PhoneNumberRoutingRepo -------------------------------------------------------------------------------


def _e164() -> str:
    return f"+1555{secrets.randbelow(10**7):07d}"


async def test_routing_finds_only_live_active_numbers(database: Database, org: uuid.UUID) -> None:
    routed, idle, gone = _e164(), _e164(), _e164()
    active_id, *_ = await _ids(
        database,
        "phone_numbers",
        org,
        [
            {"e164": routed, "status": "active"},
            {"e164": routed, "status": "released"},
            {"e164": idle, "status": "pending"},
            {"e164": idle, "status": "releasing"},
            {"e164": gone, "status": "active", "deleted_at": _BASE},
        ],
    )
    async with database.system_session() as session:
        repo = PhoneNumberRoutingRepo(session)
        found = await repo.get_active(routed)
        assert found is not None
        assert (found.id, found.org_id) == (active_id, org)
        for missing in (idle, gone, _e164()):
            assert await repo.get_active(missing) is None
    assert isinstance(found, PhoneNumber)


# ---- OutboxRepo -------------------------------------------------------------------------------------------

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


async def _deliveries(
    database: Database, org: uuid.UUID, rows: Sequence[Mapping[str, Any]]
) -> list[uuid.UUID]:
    """Clear the outbox, then insert one delivery per ``rows`` entry (status, next_attempt_at, ...)."""
    async with database.system_session() as session:
        await session.execute(delete(WebhookDelivery))
    return await _ids(
        database,
        "webhook_deliveries",
        org,
        [{"event_id": f"evt_{new_id()}", "url": "https://hooks.example.test/va", **row} for row in rows],
    )


async def _claim(database: Database, now: datetime, **kwargs: Any) -> list[WebhookDelivery]:
    async with database.system_session() as session:
        return await OutboxRepo(session).claim_due(now, **kwargs)


async def test_claim_due_takes_due_rows_under_a_lease(database: Database, org: uuid.UUID) -> None:
    pending, expired_lease, future, *_ = await _deliveries(
        database,
        org,
        [
            {"status": "pending", "next_attempt_at": _NOW - timedelta(minutes=1)},
            {"status": "delivering", "next_attempt_at": _NOW},
            {"status": "pending", "next_attempt_at": _NOW + DEFAULT_LEASE},
            {"status": "succeeded", "next_attempt_at": _NOW - timedelta(hours=1)},
            {"status": "failed", "next_attempt_at": _NOW - timedelta(hours=1)},
            {"status": "dead", "next_attempt_at": _NOW - timedelta(hours=1)},
        ],
    )
    claimed = await _claim(database, _NOW)
    assert {row.id for row in claimed} == {pending, expired_lease}
    assert {(row.status, row.next_attempt_at, row.attempts) for row in claimed} == {
        ("delivering", _NOW + DEFAULT_LEASE, 0)
    }
    assert await _claim(database, _NOW + DEFAULT_LEASE - _SECOND) == []
    recovered = await _claim(database, _NOW + DEFAULT_LEASE)
    assert {row.id for row in recovered} == {pending, expired_lease, future}


async def test_claim_due_honours_the_limit_oldest_first(database: Database, org: uuid.UUID) -> None:
    ids = await _deliveries(
        database, org, [{"next_attempt_at": _NOW - timedelta(minutes=minute)} for minute in range(5)]
    )
    assert {row.id for row in await _claim(database, _NOW, limit=2)} == set(ids[-2:])
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await _claim(database, _NOW, limit=0)


async def test_concurrent_claims_skip_locked_rows(
    database: Database, migrated_url: str, org: uuid.UUID
) -> None:
    ids = await _deliveries(
        database, org, [{"next_attempt_at": _NOW - timedelta(minutes=m)} for m in range(5)]
    )
    async with open_database(migrated_url) as other, database.system_session() as first:
        mine = await OutboxRepo(first).claim_due(_NOW, limit=2)
        async with asyncio.timeout(20), other.system_session() as second:
            theirs = await OutboxRepo(second).claim_due(_NOW, limit=10)
    assert len(mine) == 2
    assert len(theirs) == 3
    assert {row.id for row in mine} | {row.id for row in theirs} == set(ids)


async def test_attempt_outcomes_are_recorded(database: Database, org: uuid.UUID) -> None:
    retried, succeeded = await _deliveries(
        database, org, [{"status": "delivering"}, {"status": "delivering", "last_error": "timeout"}]
    )
    async with database.system_session() as session:
        repo = OutboxRepo(session)
        first = await repo.schedule_retry(
            retried,
            next_attempt_at=_NOW + timedelta(seconds=10),
            status_code=None,
            error="timeout",
            response_ms=None,
        )
        assert (first.status, first.attempts, first.next_attempt_at) == (
            "pending",
            1,
            _NOW + timedelta(seconds=10),
        )
        assert (first.last_status_code, first.last_error, first.last_response_ms) == (None, "timeout", None)
        dead = await repo.mark_dead(retried, status_code=410, error="gone", response_ms=35)
        assert (
            dead.status,
            dead.attempts,
            dead.last_status_code,
            dead.last_error,
            dead.last_response_ms,
        ) == (
            "dead",
            2,
            410,
            "gone",
            35,
        )
        done = await repo.mark_succeeded(succeeded, now=_NOW, status_code=204, response_ms=120)
        assert (done.status, done.attempts, done.delivered_at, done.last_error) == (
            "succeeded",
            1,
            _NOW,
            None,
        )
        assert (done.last_status_code, done.last_response_ms) == (204, 120)
        for outcome in (
            repo.mark_succeeded(new_id(), now=_NOW, status_code=200, response_ms=1),
            repo.schedule_retry(new_id(), next_attempt_at=_NOW, status_code=500, error="e", response_ms=1),
            repo.mark_dead(new_id(), status_code=500, error="e", response_ms=1),
        ):
            with pytest.raises(NotFoundError, match="webhook_deliveries not found"):
                await outcome


# ---- SchedulerRepo ----------------------------------------------------------------------------------------


async def test_due_scheduled_calls_in_start_order(database: Database, org: uuid.UUID) -> None:
    now = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    earliest, middle, exact, *_ = await _ids(
        database,
        "calls",
        org,
        [
            {"status": "scheduled", "start_time": now - timedelta(hours=2)},
            {"status": "scheduled", "start_time": now - timedelta(hours=1)},
            {"status": "scheduled", "start_time": now},
            {"status": "scheduled", "start_time": now + _SECOND},
            {"status": "scheduled", "start_time": None},
            {"status": "queued", "start_time": now - timedelta(hours=3)},
        ],
    )
    async with database.system_session() as session:
        repo = SchedulerRepo(session)
        assert [call.id for call in await repo.due_scheduled_calls(now)] == [earliest, middle, exact]
        assert [call.id for call in await repo.due_scheduled_calls(now, limit=2)] == [earliest, middle]
        assert all(isinstance(call, Call) for call in await repo.due_scheduled_calls(now))


# ---- RetentionRepo ----------------------------------------------------------------------------------------


async def test_purge_deletes_rows_past_each_window(database: Database, org: uuid.UUID) -> None:
    now = datetime(2020, 1, 1, tzinfo=UTC)
    expired, kept = [], []
    for rule in PURGE_RULES:
        cutoff = rule.cutoff(now)
        old, fresh = await _insert(
            database, rule.table, org, [{rule.column: cutoff - _SECOND}, {rule.column: cutoff + _SECOND}]
        )
        expired.append((rule.table, old))
        kept.append((rule.table, fresh))
    async with database.system_session() as session:
        counts = await RetentionRepo(session).purge_expired(now)
    assert counts == {rule.table: 1 for rule in PURGE_RULES}
    for table, key in expired:
        assert not await _exists(database, table, key), table
    for table, key in kept:
        assert await _exists(database, table, key), table


async def test_purge_runs_in_batches(database: Database, org: uuid.UUID) -> None:
    now = datetime(2010, 1, 1, tzinfo=UTC)
    stale = now - timedelta(days=2)
    keys = await _insert(database, "idempotency_keys", org, [{"created_at": stale}] * 5)
    async with database.system_session() as session:
        counts = await RetentionRepo(session).purge_expired(now, batch_size=2)
    assert counts["idempotency_keys"] == 5
    assert set(counts) == {rule.table for rule in PURGE_RULES}
    assert not any([await _exists(database, "idempotency_keys", key) for key in keys])


async def test_retention_policies_cover_every_org(database: Database) -> None:
    async with database.system_session() as session:
        org = await make_org(session, retention_days=30, recording_retention_days=7)
    async with database.system_session() as session:
        policies = await RetentionRepo(session).retention_policies()
    assert RetentionPolicy(org_id=org.id, retention_days=30, recording_retention_days=7) in policies
    assert [policy.org_id for policy in policies] == sorted(policy.org_id for policy in policies)


# ---- BillingReconcileRepo ---------------------------------------------------------------------------------


async def test_calls_ended_between_is_half_open_and_pages_by_id(database: Database, org: uuid.UUID) -> None:
    start, end = datetime(2021, 3, 1, tzinfo=UTC), datetime(2021, 3, 1, 1, tzinfo=UTC)
    tick = timedelta(microseconds=1)
    ids = await _ids(
        database,
        "calls",
        org,
        [
            {"status": "completed", "ended_at": stamp}
            for stamp in (start - tick, start, start + _SECOND, end - tick, end)
        ],
    )
    inside = sorted(ids[1:4])
    async with database.system_session() as session:
        repo = BillingReconcileRepo(session)
        assert [call.id for call in await repo.calls_ended_between(start, end)] == inside
        first = await repo.calls_ended_between(start, end, limit=2)
        rest = await repo.calls_ended_between(start, end, after_id=first[-1].id, limit=2)
        assert [call.id for call in first + rest] == inside


async def test_release_expired_holds(database: Database, org: uuid.UUID) -> None:
    now = datetime(2022, 2, 2, tzinfo=UTC)
    rows = [
        {"status": "held", "expires_at": now - _SECOND},
        {"status": "held", "expires_at": now},
        {"status": "held", "expires_at": now + _SECOND},
        {"status": "captured", "expires_at": now - _SECOND},
        {"status": "released", "expires_at": now - _SECOND},
    ]
    ids = await _ids(database, "credit_holds", org, rows)
    async with database.system_session() as session:
        released = await BillingReconcileRepo(session).release_expired_holds(now)
    assert {hold.id for hold in released} == set(ids[:2])
    assert {hold.status for hold in released} == {"released"}
    async with database.system_session() as session:
        repo = BillingReconcileRepo(session)
        assert await repo.release_expired_holds(now) == []
        statuses = dict(
            (
                await session.execute(select(CreditHold.id, CreditHold.status).where(CreditHold.id.in_(ids)))
            ).all()
        )
    assert [statuses[row_id] for row_id in ids] == ["released", "released", "held", "captured", "released"]
    with pytest.raises(ValueError, match="limit must be at least 1"):
        async with database.system_session() as session:
            await BillingReconcileRepo(session).release_expired_holds(now, limit=0)


async def _ledger(session: AsyncSession, org_id: uuid.UUID, *amounts: int) -> None:
    session.add_all(
        CreditLedger(
            org_id=org_id,
            entry_type="purchase",
            amount_micros=amount,
            balance_after_micros=amount,
            idempotency_key=f"test-{new_id()}",
        )
        for amount in amounts
    )


async def test_balance_drifts_compare_balance_with_the_ledger(database: Database) -> None:
    async with database.system_session() as session:
        unsynced = (await make_org(session)).id
        matching = (await make_org(session, credits_balance_micros=300)).id
        no_ledger = (await make_org(session, credits_balance_micros=100)).id
        empty = (await make_org(session)).id
        await _ledger(session, unsynced, 200, 500, -200)
        await _ledger(session, matching, 500, -200)
    mine = {unsynced, matching, no_ledger, empty}
    async with database.system_session() as session:
        repo = BillingReconcileRepo(session)
        drifts = [drift for drift in await repo.balance_drifts() if drift.org_id in mine]
        assert drifts == sorted(
            [BalanceDrift(unsynced, 0, 500), BalanceDrift(no_ledger, 100, 0)], key=lambda drift: drift.org_id
        )
        await repo.set_balance(unsynced, 500)
        await repo.set_balance(no_ledger, 0)
        with pytest.raises(NotFoundError, match="orgs not found"):
            await repo.set_balance(new_id(), 0)
    async with database.system_session() as session:
        assert not mine & {drift.org_id for drift in await BillingReconcileRepo(session).balance_drifts()}


# ---- TelnyxEventRepo --------------------------------------------------------------------------------------


async def test_telnyx_events_are_deduplicated_and_marked(database: Database, org: uuid.UUID) -> None:
    event = NewTelnyxEvent(
        telnyx_event_id=f"tx-{new_id()}",
        event_type="call.initiated",
        occurred_at=_BASE,
        payload={"data": {"event_type": "call.initiated"}},
        org_id=org,
        call_control_id="v3:abc",
    )
    async with database.system_session() as session:
        repo = TelnyxEventRepo(session)
        stored = await repo.record(event)
        assert stored is not None
        assert (stored.telnyx_event_id, stored.org_id, stored.call_id, stored.processed_at) == (
            event.telnyx_event_id,
            org,
            None,
            None,
        )
        assert await repo.record(event) is None
        count = select(func.count()).where(TelnyxEvent.telnyx_event_id == event.telnyx_event_id)
        assert await session.scalar(count) == 1
        await repo.mark_processed(stored.id, now=_NOW, error="no call for ccid")
        with pytest.raises(NotFoundError, match="telnyx_events not found"):
            await repo.mark_processed(new_id(), now=_NOW)
    async with database.system_session() as session:
        await TelnyxEventRepo(session).mark_processed(stored.id, now=_NOW + _SECOND)
        row = await session.get_one(TelnyxEvent, stored.id)
        assert (row.processed_at, row.process_error) == (_NOW + _SECOND, None)
