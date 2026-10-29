"""Tenant and system sessions (specs/03-data-model.md §2, §10).

Every tenant query runs inside :func:`tenant_session`, which opens a transaction on the ``va_app`` engine and
sets ``app.org_id`` transaction-locally so row-level security confines it to one org. The setting ends with
the transaction, so a pooled connection never carries it into the next session. :func:`global_session` reaches
the identity tables without ``org_id`` (``orgs``, ``users``, ``sessions``, ``email_tokens``; specs/03 §2).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from va_common.settings import DatabaseSettings
from va_db.engine import DbRole, create_engine

__all__ = [
    "Database",
    "configure_database",
    "get_database",
    "global_session",
    "system_session",
    "tenant_session",
]

_SET_ORG_ID = text("SELECT set_config('app.org_id', :org_id, true)")


class Database:
    """The ``va_app`` and ``va_system`` engines of one process and their session factories."""

    def __init__(self, app_engine: AsyncEngine, system_engine: AsyncEngine) -> None:
        self.app_engine = app_engine
        self.system_engine = system_engine
        self._app_sessions = async_sessionmaker(app_engine, expire_on_commit=False)
        self._system_sessions = async_sessionmaker(system_engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> Self:
        """Engines for ``DATABASE_URL`` assuming ``va_app`` and ``va_system`` respectively."""
        return cls(create_engine(settings, DbRole.APP), create_engine(settings, DbRole.SYSTEM))

    async def dispose(self) -> None:
        """Close every pooled connection of both engines."""
        await self.app_engine.dispose()
        await self.system_engine.dispose()

    @asynccontextmanager
    async def tenant_session(self, org_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """A transaction as ``va_app`` with ``app.org_id`` set; commits on success, rolls back on error."""
        async with self._app_sessions() as session, session.begin():
            await session.execute(_SET_ORG_ID, {"org_id": str(org_id)})
            yield session

    @asynccontextmanager
    async def global_session(self) -> AsyncIterator[AsyncSession]:
        """A transaction as ``va_app`` without ``app.org_id``; commits on success, rolls back on error.

        Only for the tables without ``org_id`` and RLS (``orgs``, ``users``, ``sessions``, ``email_tokens``):
        every RLS table reads as empty and rejects writes here (specs/03-data-model.md §2).
        """
        async with self._app_sessions() as session, session.begin():
            yield session

    @asynccontextmanager
    async def system_session(self) -> AsyncIterator[AsyncSession]:
        """A transaction as ``va_system`` (``BYPASSRLS``); commits on success, rolls back on error.

        Only the system-scope repositories listed in specs/03-data-model.md §2 may use it; any new caller
        must cite that section in a comment.
        """
        async with self._system_sessions() as session, session.begin():
            yield session


class _Registry:
    database: Database | None = None


def configure_database(database: Database | None) -> None:
    """Install (or, with ``None``, remove) the process-wide :class:`Database` used by the module helpers."""
    _Registry.database = database


def get_database() -> Database:
    """The process-wide :class:`Database`; ``RuntimeError`` before :func:`configure_database`."""
    database = _Registry.database
    if database is None:
        msg = "va_db is not configured; call configure_database() at startup"
        raise RuntimeError(msg)
    return database


@asynccontextmanager
async def tenant_session(org_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """:meth:`Database.tenant_session` of the process-wide database."""
    async with get_database().tenant_session(org_id) as session:
        yield session


@asynccontextmanager
async def global_session() -> AsyncIterator[AsyncSession]:
    """:meth:`Database.global_session` of the process-wide database (identity tables only)."""
    async with get_database().global_session() as session:
        yield session


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """:meth:`Database.system_session` of the process-wide database (specs/03-data-model.md §2 only)."""
    async with get_database().system_session() as session:
        yield session
