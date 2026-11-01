"""Shared fixtures: one migrated Postgres per xdist worker for the DB-backed billing tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from billing_helpers import LimiterRig, limiter_rig
from migration_helpers import migrated_postgres
from tenant_rows import open_database

from va_db.session import Database


@pytest.fixture(scope="session")
def billing_db_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url


@pytest.fixture
async def database(billing_db_url: str) -> AsyncIterator[Database]:
    async with open_database(billing_db_url) as db:
        yield db


@pytest.fixture
async def rig() -> AsyncIterator[LimiterRig]:
    built = limiter_rig()
    try:
        yield built
    finally:
        await built.redis.aclose()
