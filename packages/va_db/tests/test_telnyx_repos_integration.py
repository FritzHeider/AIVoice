"""``TelnyxEventRepo`` reprocessing and ``TelnyxAccountLookupRepo`` on a migrated database (specs/03 §2, §4;
specs/06 §4.1).

``updated_at`` is set by the ``set_updated_at`` trigger from the database clock, so the claim windows are
driven by passing ``now`` values relative to the real time.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from tenant_rows import OrgFactory, open_database

from va_common.ids import new_id
from va_db.models import TelnyxAccount, TelnyxEvent
from va_db.repositories import NewTelnyxEvent, TelnyxAccountLookupRepo, TelnyxEventRepo, TelnyxWebhookKey
from va_db.session import Database

pytestmark = pytest.mark.integration

_MINUTE = timedelta(minutes=1)


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


def _event(**values: object) -> NewTelnyxEvent:
    defaults: dict[str, object] = {
        "telnyx_event_id": f"tx-{new_id()}",
        "event_type": "call.answered",
        "occurred_at": datetime.now(UTC),
        "payload": {"data": {"event_type": "call.answered"}},
    }
    return NewTelnyxEvent(**{**defaults, **values})  # type: ignore[arg-type]


async def _record(database: Database, **values: object) -> TelnyxEvent:
    async with database.system_session() as session:
        stored = await TelnyxEventRepo(session).record(_event(**values))
    assert stored is not None
    return stored


async def _row(database: Database, event_id: uuid.UUID) -> TelnyxEvent:
    async with database.system_session() as session:
        return await session.get_one(TelnyxEvent, event_id)


async def _claim(database: Database, now: datetime, **knobs: object) -> set[uuid.UUID]:
    policy: dict[str, object] = {"stale_after": timedelta(seconds=30), "retry_after": timedelta(seconds=5)}
    async with database.system_session() as session:
        claimed = await TelnyxEventRepo(session).claim_retries(
            now=now,
            max_attempts=5,
            **{**policy, **knobs},  # type: ignore[arg-type]
        )
    return {row.id for row in claimed}


async def test_record_keeps_an_existing_org_and_nulls_a_missing_one(database: Database) -> None:
    org = await OrgFactory(database).create()
    kept = await _record(database, org_id=org, call_id=new_id())
    dropped = await _record(database, org_id=new_id())
    assert (await _row(database, kept.id)).org_id == org
    assert (await _row(database, dropped.id)).org_id is None


async def test_mark_processed_counts_attempts(database: Database) -> None:
    stored = await _record(database)
    assert stored.process_attempts == 0
    now = datetime.now(UTC)
    async with database.system_session() as session:
        await TelnyxEventRepo(session).mark_processed(stored.id, now=now, error="RuntimeError: boom")
    async with database.system_session() as session:
        await TelnyxEventRepo(session).mark_processed(stored.id, now=now)
    row = await _row(database, stored.id)
    assert (row.process_attempts, row.process_error, row.processed_at) == (2, None, now)


async def test_claim_retries_takes_stale_unprocessed_and_failed_events(database: Database) -> None:
    unprocessed = await _record(database)
    failed = await _record(database)
    done = await _record(database)
    exhausted = await _record(database)
    now = datetime.now(UTC)
    async with database.system_session() as session:
        repo = TelnyxEventRepo(session)
        await repo.mark_processed(failed.id, now=now, error="RuntimeError: boom")
        await repo.mark_processed(done.id, now=now)
        await repo.mark_processed(exhausted.id, now=now, error="RuntimeError: boom")
        await session.execute(
            update(TelnyxEvent).where(TelnyxEvent.id == exhausted.id).values(process_attempts=5)
        )
    mine = {unprocessed.id, failed.id, done.id, exhausted.id}

    assert not await _claim(database, datetime.now(UTC)) & mine
    claimed = await _claim(database, datetime.now(UTC) + _MINUTE)
    assert claimed & mine == {unprocessed.id, failed.id}
    # Claiming touched the rows: a second claim inside the windows skips them.
    assert not await _claim(database, datetime.now(UTC) + timedelta(seconds=4)) & mine
    assert await _claim(database, datetime.now(UTC) + _MINUTE) & mine == {unprocessed.id, failed.id}


async def test_claim_retries_respects_the_limit_oldest_first(database: Database) -> None:
    older = await _record(database)
    newer = await _record(database)
    now = datetime.now(UTC) + 10 * _MINUTE
    assert len(await _claim(database, now, limit=1)) == 1
    async with database.system_session() as session:
        claimed = await TelnyxEventRepo(session).claim_retries(
            now=now, stale_after=_MINUTE, retry_after=_MINUTE, max_attempts=5
        )
    ids = [row.id for row in claimed]
    assert ids.index(older.id) < ids.index(newer.id)
    assert [row.created_at for row in claimed] == sorted(row.created_at for row in claimed)


async def test_claim_retries_rejects_non_positive_knobs(database: Database) -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="limit must be at least 1"):
        await _claim(database, now, limit=0)
    with pytest.raises(ValueError, match="max_attempts must be at least 1"):
        async with database.system_session() as session:
            await TelnyxEventRepo(session).claim_retries(
                now=now, stale_after=_MINUTE, retry_after=_MINUTE, max_attempts=0
            )


async def _account(database: Database, org: uuid.UUID, status: str) -> uuid.UUID:
    public_key = secrets.token_hex(16)
    async with database.system_session() as session:
        account = TelnyxAccount(
            org_id=org,
            label="byo",
            api_key_enc=os.urandom(16),
            public_key=public_key,
            encrypted_key_handle=f"tk-{secrets.token_hex(8)}",
            status=status,
        )
        session.add(account)
        await session.flush()
        return account.id


@pytest.mark.parametrize("status", ["pending", "active", "error"])
async def test_webhook_key_of_a_live_account(database: Database, status: str) -> None:
    org = await OrgFactory(database).create()
    account_id = await _account(database, org, status)
    async with database.system_session() as session:
        key = await TelnyxAccountLookupRepo(session).webhook_key(account_id)
        account = await session.get_one(TelnyxAccount, account_id)
    assert key == TelnyxWebhookKey(public_key=account.public_key, org_id=org)


async def test_webhook_key_of_deleted_or_unknown_account_is_none(database: Database) -> None:
    org = await OrgFactory(database).create()
    deleted = await _account(database, org, "deleted")
    async with database.system_session() as session:
        repo = TelnyxAccountLookupRepo(session)
        assert await repo.webhook_key(deleted) is None
        assert await repo.webhook_key(new_id()) is None
