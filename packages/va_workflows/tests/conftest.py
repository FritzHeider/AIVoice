"""Shared fixtures: one migrated Postgres per xdist worker for the DB-backed activity tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from migration_helpers import migrated_postgres
from service_helpers import isolate_env, reset_logging
from tenant_rows import open_database

from va_common.settings import DatabaseSettings, RedisSettings, StreamTokenSettings, TemporalSettings
from va_db.session import Database

__all__ = ["reset_logging"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_env(monkeypatch, tmp_path, DatabaseSettings, RedisSettings, StreamTokenSettings, TemporalSettings)


@pytest.fixture(scope="session")
def workflows_db_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url


@pytest.fixture
async def database(workflows_db_url: str) -> AsyncIterator[Database]:
    async with open_database(workflows_db_url) as db:
        yield db
