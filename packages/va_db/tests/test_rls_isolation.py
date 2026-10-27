"""Row-level security isolation over every tenant table (specs/03-data-model.md §10; specs/00 §4 rule 7).

Semantics: inside ``tenant_session(A)`` a row of org B is invisible. Postgres RLS ``USING`` filters rows,
it does not raise, so SELECT returns nothing and UPDATE/DELETE of B's row affect 0 rows. Writes that would
*produce* a row outside A raise ``new row violates row-level security policy`` (``WITH CHECK``): an INSERT
with ``org_id = B`` and an UPDATE that moves A's own row to B. Afterwards B's row is unchanged when read in
``tenant_session(B)``. Platform rows (``org_id IS NULL``) of ``voices`` and ``dnc_entries`` are readable by
every org and writable by none."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from migration_helpers import query
from sqlalchemy import ColumnElement, Executable, and_, delete, insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from tenant_rows import (
    PLATFORM_READ_TABLES,
    TENANT_TABLE_NAMES,
    OrgFactory,
    RowFactory,
    RowKey,
    open_database,
)

from va_db.base import Base
from va_db.session import Database

pytestmark = pytest.mark.integration

_RLS_TABLES = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND c.relrowsecurity AND c.relforcerowsecurity
  AND EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = 'tenant_isolation')
ORDER BY 1
"""
_PLATFORM_READ_TABLES = "SELECT tablename FROM pg_policies WHERE policyname = 'platform_read' ORDER BY 1"


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


def _matches(table_name: str, key: RowKey) -> ColumnElement[bool]:
    table = Base.metadata.tables[table_name]
    return and_(*(table.c[column] == value for column, value in key.items()))


async def _keys(
    executor: AsyncSession | AsyncConnection, table_name: str, where: ColumnElement[bool]
) -> list[RowKey]:
    table = Base.metadata.tables[table_name]
    rows = await executor.execute(select(*table.primary_key.columns).where(where))
    return [dict(row._mapping) for row in rows]


async def _visible_org_ids(
    executor: AsyncSession | AsyncConnection, table_name: str
) -> set[uuid.UUID | None]:
    table = Base.metadata.tables[table_name]
    return set((await executor.scalars(select(table.c.org_id).distinct())).all())


async def _assert_rls_rejects(session: AsyncSession, statement: Executable) -> None:
    with pytest.raises(DBAPIError, match="row-level security policy"):
        async with session.begin_nested():
            await session.execute(statement)


async def _rowcount(session: AsyncSession, statement: Executable) -> int:
    result: Any = await session.execute(statement)
    count: int = result.rowcount
    return count


def test_rls_tables_are_the_tenant_tables(migrated_url: str) -> None:
    assert [row[0] for row in query(migrated_url, _RLS_TABLES)] == list(TENANT_TABLE_NAMES)
    assert [row[0] for row in query(migrated_url, _PLATFORM_READ_TABLES)] == list(PLATFORM_READ_TABLES)


async def _assert_sees_only_own(
    session: AsyncSession, table_name: str, own: tuple[uuid.UUID, RowKey], other: tuple[uuid.UUID, RowKey]
) -> None:
    """The session sees its org's row (plus platform rows where allowed) and nothing of the other org."""
    table = Base.metadata.tables[table_name]
    (own_org, own_key), (other_org, other_key) = own, other
    assert await _visible_org_ids(session, table_name) <= {own_org} | _shared(table_name)
    assert await _keys(session, table_name, table.c.org_id == own_org) == [own_key]
    assert await _keys(session, table_name, table.c.org_id == other_org) == []
    assert await _keys(session, table_name, _matches(table_name, other_key)) == []


async def _assert_cannot_touch(session: AsyncSession, table_name: str, key: RowKey) -> None:
    """UPDATE and DELETE of an invisible row affect nothing (RLS ``USING`` filters, it does not raise)."""
    table = Base.metadata.tables[table_name]
    row = _matches(table_name, key)
    assert await _rowcount(session, update(table).where(row).values(org_id=table.c.org_id)) == 0
    assert await _rowcount(session, delete(table).where(row)) == 0


def _shared(table_name: str) -> set[uuid.UUID | None]:
    return {None} if table_name in PLATFORM_READ_TABLES else set()


@pytest.mark.parametrize("table_name", TENANT_TABLE_NAMES)
async def test_cross_org_access_is_impossible(database: Database, table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    orgs, rows = OrgFactory(database), RowFactory()
    org_a, org_b = await orgs.create(), await orgs.create()
    async with database.tenant_session(org_a) as session:
        a_key = await rows.insert(session, table, org_a)
    async with database.tenant_session(org_b) as session:
        b_key = await rows.insert(session, table, org_b)
        b_values = await rows.values(session, table, org_b)

    async with database.tenant_session(org_a) as session:
        await _assert_sees_only_own(session, table_name, (org_a, a_key), (org_b, b_key))
        await _assert_rls_rejects(session, insert(table).values(**b_values))
        await _assert_cannot_touch(session, table_name, b_key)
        await _assert_rls_rejects(
            session, update(table).where(_matches(table_name, a_key)).values(org_id=org_b)
        )

    async with database.tenant_session(org_b) as session:
        await _assert_sees_only_own(session, table_name, (org_b, b_key), (org_a, a_key))

    async with database.app_engine.connect() as connection:
        assert await _visible_org_ids(connection, table_name) <= _shared(table_name)


@pytest.mark.parametrize("table_name", PLATFORM_READ_TABLES)
async def test_platform_rows_are_shared_read_only(database: Database, table_name: str) -> None:
    table = Base.metadata.tables[table_name]
    orgs, rows = OrgFactory(database), RowFactory()
    org_a, org_b = await orgs.create(), await orgs.create()
    async with database.system_session() as session:
        platform_key = await rows.insert(session, table, None)
    platform_row = _matches(table_name, platform_key)

    for org in (org_a, org_b):
        async with database.tenant_session(org) as session:
            assert await _keys(session, table_name, platform_row) == [platform_key]

    async with database.tenant_session(org_a) as session:
        forged = await rows.values(session, table, None)
        await _assert_rls_rejects(session, insert(table).values(**forged))
        await _assert_cannot_touch(session, table_name, platform_key)

    async with database.system_session() as session:
        assert await _keys(session, table_name, platform_row) == [platform_key]
