"""Shared fixtures: one migrated Postgres per xdist worker for the DB-backed compliance tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from migration_helpers import migrated_postgres
from tenant_rows import open_database

from va_db.session import Database


@pytest.fixture(scope="session")
def compliance_db_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url


@pytest.fixture
async def database(compliance_db_url: str) -> AsyncIterator[Database]:
    async with open_database(compliance_db_url) as db:
        yield db
