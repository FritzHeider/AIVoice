"""Telephony edge test fixtures: isolated environment, log reset, and a migrated Postgres per worker."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from migration_helpers import migrated_postgres
from service_helpers import isolate_env, reset_logging
from tenant_rows import open_database

from va_db.session import Database
from va_edge.settings import EdgeSettings

__all__ = ["reset_logging"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_env(monkeypatch, tmp_path, EdgeSettings)


@pytest.fixture(scope="session")
def edge_db_url() -> Iterator[str]:
    with migrated_postgres() as url:
        yield url


@pytest.fixture
async def database(edge_db_url: str) -> AsyncIterator[Database]:
    async with open_database(edge_db_url) as db:
        yield db
