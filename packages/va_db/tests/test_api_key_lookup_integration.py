"""``ApiKeyLookupRepo`` against a migrated database (specs/03-data-model.md §2; specs/04 §2.2).

The repository runs on ``system_session`` (``va_system``, ``BYPASSRLS``): no ``app.org_id`` is set, yet keys
of every org are found by prefix.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from model_factories import flushed, make_org
from sqlalchemy import select
from tenant_rows import open_database

from va_db.models import ApiKey
from va_db.repositories import ApiKeyLookupRepo, ApiKeyRecord
from va_db.session import Database

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


def _prefix() -> str:
    return f"va_live_{secrets.token_hex(2)}"


async def _make_key(
    database: Database, *, org_status: str = "active", revoked_at: datetime | None = None
) -> tuple[uuid.UUID, uuid.UUID, str]:
    prefix = _prefix()
    async with database.system_session() as session:
        org = await make_org(session, status=org_status, plan_id="build")
        key = await flushed(
            session,
            ApiKey(
                org_id=org.id,
                name="server",
                prefix=prefix,
                secret_hash="ab" * 32,
                scopes=["calls:read", "calls:write"],
                pepper_version=2,
                environment="test",
                expires_at=_NOW + timedelta(days=30),
                revoked_at=revoked_at,
            ),
        )
        return org.id, key.id, prefix


async def test_get_by_prefix_returns_key_with_org_status_and_plan(database: Database) -> None:
    org_id, key_id, prefix = await _make_key(database, org_status="suspended")
    async with database.system_session() as session:
        record = await ApiKeyLookupRepo(session).get_by_prefix(prefix)
    assert record == ApiKeyRecord(
        id=key_id,
        org_id=org_id,
        secret_hash="ab" * 32,
        scopes=("calls:read", "calls:write"),
        environment="test",
        pepper_version=2,
        revoked=False,
        expires_at=_NOW + timedelta(days=30),
        org_status="suspended",
        plan_id="build",
    )


async def test_get_by_prefix_unknown_and_revoked(database: Database) -> None:
    _, _, prefix = await _make_key(database, revoked_at=_NOW)
    async with database.system_session() as session:
        repo = ApiKeyLookupRepo(session)
        revoked = await repo.get_by_prefix(prefix)
        assert await repo.get_by_prefix(_prefix()) is None
    assert revoked is not None
    assert revoked.revoked


async def test_touch_sets_last_used_at(database: Database) -> None:
    _, key_id, _ = await _make_key(database)
    async with database.system_session() as session:
        await ApiKeyLookupRepo(session).touch(key_id, _NOW)
    async with database.system_session() as session:
        assert await session.scalar(select(ApiKey.last_used_at).where(ApiKey.id == key_id)) == _NOW
