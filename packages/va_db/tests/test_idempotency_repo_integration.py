"""``IdempotencyRepo`` against a migrated database (specs/04-auth-tenancy-rbac.md §12, specs/03 §3).

The repository runs on ``tenant_session`` (``va_app``, RLS): rows of another org are invisible even by key.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from service_helpers import settings_of
from sqlalchemy import func, select
from tenant_rows import OrgFactory, open_database

from va_common.settings import DatabaseSettings
from va_db.models import IdempotencyKey
from va_db.repositories import (
    LOCK_TTL,
    REPLAY_WINDOW,
    ClaimOutcome,
    IdempotencyClaim,
    IdempotencyConflict,
    IdempotencyReplay,
    IdempotencyRepo,
)
from va_db.session import Database

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_PATH = "/v1/calls"
_HASH = "a" * 64


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


async def _org(database: Database) -> uuid.UUID:
    return await OrgFactory(database).create()


async def _claim(
    database: Database, org_id: uuid.UUID, key: str, *, now: datetime = _NOW, request_hash: str = _HASH
) -> ClaimOutcome:
    async with database.tenant_session(org_id) as session:
        return await IdempotencyRepo(session, org_id).claim(
            key=key, method="POST", path=_PATH, request_hash=request_hash, now=now
        )


async def _complete(database: Database, org_id: uuid.UUID, key: str, status: int, body: object) -> None:
    async with database.tenant_session(org_id) as session:
        await IdempotencyRepo(session, org_id).complete(key, status=status, body=body, now=_NOW)


async def _row(database: Database, org_id: uuid.UUID, key: str) -> IdempotencyKey | None:
    async with database.tenant_session(org_id) as session:
        statement = select(IdempotencyKey).where(IdempotencyKey.org_id == org_id, IdempotencyKey.key == key)
        return (await session.execute(statement)).scalar_one_or_none()


async def test_claim_inserts_locked_row(database: Database) -> None:
    org_id = await _org(database)
    assert await _claim(database, org_id, "k1") == IdempotencyClaim("k1")
    row = await _row(database, org_id, "k1")
    assert row is not None
    assert (row.method, row.path, row.request_hash) == ("POST", _PATH, _HASH)
    assert row.created_at == _NOW
    assert row.locked_until == _NOW + LOCK_TTL
    assert row.response_status is None
    assert row.response_body is None


async def test_complete_then_replay(database: Database) -> None:
    org_id = await _org(database)
    await _claim(database, org_id, "k1")
    body = {"status": "success", "call_id": "c1", "nested": {"b": 1, "a": [1, 2]}}
    await _complete(database, org_id, "k1", 201, body)
    row = await _row(database, org_id, "k1")
    assert row is not None
    assert row.locked_until is None
    assert await _claim(database, org_id, "k1", now=_NOW + timedelta(hours=1)) == IdempotencyReplay(201, body)


async def test_hash_mismatch_is_key_reused_even_while_locked(database: Database) -> None:
    org_id = await _org(database)
    await _claim(database, org_id, "k1")
    outcome = await _claim(database, org_id, "k1", request_hash="b" * 64)
    assert outcome is IdempotencyConflict.KEY_REUSED
    await _complete(database, org_id, "k1", 200, {"ok": True})
    assert await _claim(database, org_id, "k1", request_hash="b" * 64) is IdempotencyConflict.KEY_REUSED


async def test_locked_row_is_in_progress_until_lock_expires(database: Database) -> None:
    org_id = await _org(database)
    await _claim(database, org_id, "k1")
    still_locked = _NOW + LOCK_TTL - timedelta(seconds=1)
    assert await _claim(database, org_id, "k1", now=still_locked) is IdempotencyConflict.IN_PROGRESS
    later = _NOW + LOCK_TTL
    assert await _claim(database, org_id, "k1", now=later) == IdempotencyClaim("k1")
    row = await _row(database, org_id, "k1")
    assert row is not None
    assert row.locked_until == later + LOCK_TTL
    assert row.created_at == _NOW


async def test_row_older_than_replay_window_is_reclaimed(database: Database) -> None:
    org_id = await _org(database)
    await _claim(database, org_id, "k1")
    await _complete(database, org_id, "k1", 201, {"n": 1})
    later = _NOW + REPLAY_WINDOW + timedelta(seconds=1)
    assert await _claim(database, org_id, "k1", now=later, request_hash="c" * 64) == IdempotencyClaim("k1")
    row = await _row(database, org_id, "k1")
    assert row is not None
    assert (row.request_hash, row.created_at, row.locked_until) == ("c" * 64, later, later + LOCK_TTL)
    assert row.response_status is None
    assert row.response_body is None


async def test_release_deletes_only_unfinished_rows(database: Database) -> None:
    org_id = await _org(database)
    await _claim(database, org_id, "open")
    await _claim(database, org_id, "done")
    await _complete(database, org_id, "done", 200, [1, 2])
    async with database.tenant_session(org_id) as session:
        repo = IdempotencyRepo(session, org_id)
        await repo.release("open")
        await repo.release("done")
    assert await _row(database, org_id, "open") is None
    assert await _row(database, org_id, "done") is not None
    assert await _claim(database, org_id, "open") == IdempotencyClaim("open")


async def test_concurrent_claims_one_winner(migrated_url: str) -> None:
    settings = settings_of(DatabaseSettings, database_url=migrated_url, database_pool_size=4)
    database = Database.from_settings(settings)
    try:
        org_id = await _org(database)
        outcomes = await asyncio.gather(*(_claim(database, org_id, "race") for _ in range(4)))
    finally:
        await database.dispose()
    assert sorted(map(str, outcomes)) == sorted(
        [str(IdempotencyClaim("race"))] + [str(IdempotencyConflict.IN_PROGRESS)] * 3
    )


async def test_rows_isolated_by_org(database: Database) -> None:
    org_a, org_b = await _org(database), await _org(database)
    await _claim(database, org_a, "shared")
    await _complete(database, org_a, "shared", 201, {"org": "a"})
    assert await _claim(database, org_b, "shared", request_hash="d" * 64) == IdempotencyClaim("shared")
    async with database.tenant_session(org_b) as session:
        visible = await session.scalar(
            select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.key == "shared")
        )
        repo_a_from_b = IdempotencyRepo(session, org_a)
        await repo_a_from_b.release("shared")
    assert visible == 1
    assert await _claim(database, org_a, "shared") == IdempotencyReplay(201, {"org": "a"})
