"""Isolate va_db tests from the developer environment and reset the process-wide database."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from docker_probe import docker_available
from pg_container import running_postgres
from service_helpers import isolate_env, settings_of
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import va_db.models  # noqa: F401  (registers every table on Base.metadata)
from va_common.settings import DatabaseSettings
from va_db.base import Base
from va_db.engine import create_engine
from va_db.session import configure_database


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    isolate_env(monkeypatch, tmp_path, DatabaseSettings)
    yield
    configure_database(None)


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """One disposable Postgres per test session (per xdist worker), shared by every integration module."""
    if not docker_available():
        pytest.skip("docker unavailable")
    with running_postgres() as url:
        yield url


@pytest.fixture
async def scratch_session(pg_url: str) -> AsyncIterator[AsyncSession]:
    """Every model table, created inside one login-role transaction that is rolled back at teardown."""
    engine = create_engine(settings_of(DatabaseSettings, database_url=pg_url, database_pool_size=1), None)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.run_sync(Base.metadata.create_all)
            async with AsyncSession(bind=connection, join_transaction_mode="create_savepoint") as scratch:
                yield scratch
            await transaction.rollback()
    finally:
        await engine.dispose()
