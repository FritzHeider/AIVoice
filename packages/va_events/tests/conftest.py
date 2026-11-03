"""va_events test fixtures: structlog reset and a migrated Postgres per module."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from migration_helpers import migrated_postgres
from service_helpers import reset_logging
from tenant_rows import open_database

from va_db.session import Database

__all__ = ["database", "migrated_url", "reset_logging"]


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    """A migrated Postgres for the requesting module (booted only by modules that use it)."""
    with migrated_postgres() as url:
        yield url


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    """A ``Database`` on :func:`migrated_url`."""
    async with open_database(migrated_url) as db:
        yield db
