"""Isolate va_db tests from the developer environment and reset the process-wide database."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from docker_probe import docker_available
from identity_db import IdentityDb, open_identity_db
from migration_helpers import fresh_database, upgrade
from pg_container import running_postgres
from service_helpers import isolate_env, settings_of
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tenant_rows import OrgFactory, open_database

import va_db.models  # noqa: F401  (registers every table on Base.metadata)
from va_common.settings import DatabaseSettings
from va_db.base import Base
from va_db.engine import create_engine
from va_db.session import Database, configure_database


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


@pytest.fixture(scope="module")
def migrated_url(pg_url: str) -> Iterator[str]:
    """One database migrated to head per test module, for read-mostly migration and schema tests."""
    with fresh_database(pg_url) as url:
        upgrade(url)
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


@pytest.fixture
async def identity_db(migrated_url: str) -> AsyncIterator[IdentityDb]:
    """A migrated database with an envelope (random local master key) and an org provisioner."""
    async with open_identity_db(migrated_url) as db:
        yield db


@pytest.fixture
async def tenant_db(migrated_url: str) -> AsyncIterator[Database]:
    """A :class:`Database` on the module's migrated database (``va_app`` and ``va_system`` pools)."""
    async with open_database(migrated_url) as db:
        yield db


@pytest.fixture
async def fresh_org(tenant_db: Database) -> uuid.UUID:
    """A fresh org on ``tenant_db`` (other tests' rows stay invisible to it)."""
    return await OrgFactory(tenant_db).create()
