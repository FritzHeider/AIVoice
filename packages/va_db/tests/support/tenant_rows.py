"""Minimal valid rows for every tenant table, built from model metadata, for the RLS isolation tests.

:class:`RowFactory` fills only the columns a row needs (NOT NULL, no default, not computed): ``org_id``
with the org under test, required foreign keys with freshly inserted parent rows, everything else with a
generic value for the column type. A CHECK that the generic value violates needs a named entry in
:data:`VALUE_OVERRIDES`; an unknown column type raises ``TypeError``, so a new table fails loudly instead
of being skipped.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from model_factories import make_org
from pgvector.sqlalchemy import Vector
from service_helpers import settings_of
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Table,
    Uuid,
    insert,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import TypeEngine

import va_db.models  # noqa: F401  (registers every table on Base.metadata)
from va_common.ids import new_id
from va_common.settings import DatabaseSettings
from va_db.base import Base
from va_db.models.library import EMBEDDING_DIMENSIONS
from va_db.session import Database

__all__ = [
    "FIXED_TIME",
    "PLATFORM_READ_TABLES",
    "TENANT_TABLE_NAMES",
    "VALUE_OVERRIDES",
    "OrgFactory",
    "RowFactory",
    "RowKey",
    "column_value",
    "open_database",
    "tenant_table_names",
]

type RowKey = dict[str, Any]
"""Primary-key column name to value; the identity of one row (composite keys included)."""

PLATFORM_READ_TABLES: Final = ("dnc_entries", "voices")
"""Tables whose ``org_id`` is nullable and whose NULL-org platform rows every org may read (specs/03 §10)."""

FIXED_TIME: Final = datetime(2026, 1, 1, tzinfo=UTC)


def _hex() -> str:
    return secrets.token_hex(6)


VALUE_OVERRIDES: Final[dict[tuple[str, str], Callable[[], Any]]] = {
    # dispositions_tag_check and org_secrets_name_check: an upper-case identifier
    ("dispositions", "tag"): lambda: f"T{_hex().upper()}",
    ("org_secrets", "name"): lambda: f"T{_hex().upper()}",
    # tools_name_check: a letter, then letters, digits or underscores
    ("tools", "name"): lambda: f"t{_hex()}",
    # agent_versions_semver_check: semantic version
    ("agent_versions", "semver"): lambda: "1.0.0",
    # voice_ratings_rating_check: rating BETWEEN 1 AND 5
    ("voice_ratings", "rating"): lambda: 3,
    # experiment_arms_weight_check: weight BETWEEN 0 AND 100
    ("experiment_arms", "weight"): lambda: 50,
}


def tenant_table_names() -> tuple[str, ...]:
    """Sorted tables under ``tenant_isolation`` by the specs/03 §10 rule: NOT NULL ``org_id`` plus the
    platform-read tables. Derived from metadata, so a new tenant table is covered automatically."""
    names = {
        table.name
        for table in Base.metadata.tables.values()
        if "org_id" in table.c and not table.c.org_id.nullable
    }
    return tuple(sorted(names | set(PLATFORM_READ_TABLES)))


TENANT_TABLE_NAMES: Final = tenant_table_names()


def _is_required(column: Column[Any]) -> bool:
    return not (
        column.nullable or column.server_default is not None or column.default is not None or column.computed
    )


def _check_sql(table: Table, name: str) -> str:
    """SQL text of the CHECK constraint ``name`` on ``table``; empty when there is none."""
    checks = [c for c in table.constraints if isinstance(c, CheckConstraint)]
    return next((str(c.sqltext) for c in checks if c.name == name), "")


def _enum_value(table: Table, column: Column[Any]) -> str | None:
    """First allowed value of ``<table>_<column>_check`` when it is an ``<column> IN (...)`` enumeration."""
    sql = _check_sql(table, f"{table.name}_{column.name}_check")
    match = re.match(rf"{column.name} IN \('([^']*)'", sql)
    return match.group(1) if match else None


def _text(column: Column[Any]) -> str:
    return f"{column.name}-{_hex()}"


_TYPE_VALUES: Final[tuple[tuple[type[TypeEngine[Any]], Callable[[Column[Any]], Any]], ...]] = (
    (Vector, lambda _: [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)),  # not a zero vector: cosine hnsw
    (CITEXT, lambda _: f"{_hex()}@example.test"),
    (String, _text),
    (Uuid, lambda _: new_id()),
    (Integer, lambda _: 1),
    (Numeric, lambda _: Decimal(1)),
    (Boolean, lambda _: False),
    (DateTime, lambda _: FIXED_TIME),
    (JSONB, lambda _: {}),
    (ARRAY, lambda _: []),
    (LargeBinary, lambda _: secrets.token_bytes(16)),
)


def column_value(table: Table, column: Column[Any]) -> Any:
    """A value for ``table.column`` that satisfies its type and CHECKs: override, enum, then by type."""
    override = VALUE_OVERRIDES.get((table.name, column.name))
    if override is not None:
        return override()
    enum_value = _enum_value(table, column)
    if enum_value is not None:
        return enum_value
    for type_, build in _TYPE_VALUES:
        if isinstance(column.type, type_):
            return build(column)
    msg = f"no generic value for {table.name}.{column.name} ({column.type!r}); add it to tenant_rows"
    raise TypeError(msg)


@asynccontextmanager
async def open_database(url: str) -> AsyncIterator[Database]:
    """A :class:`Database` on ``url`` with one pooled connection per role, disposed on exit."""
    database = Database.from_settings(settings_of(DatabaseSettings, database_url=url, database_pool_size=1))
    try:
        yield database
    finally:
        await database.dispose()


class OrgFactory:
    """Creates orgs through ``system_session`` (``orgs`` has no RLS and tenant sessions need an org first)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self) -> uuid.UUID:
        async with self._database.system_session() as session:
            return (await make_org(session)).id


class RowFactory:
    """Builds and inserts minimal valid rows of any table; parents are inserted fresh in the same session."""

    async def values(self, session: AsyncSession, table: Table, org_id: uuid.UUID | None) -> dict[str, Any]:
        """Values for every required column of ``table`` (plus ``org_id`` whenever the table has one).

        Required foreign keys point to parent rows inserted here, owned by ``org_id``; ``table`` itself is not
        inserted. Python-side defaults (``id``) are applied by the INSERT.
        """
        values: dict[str, Any] = {}
        for column in table.c:
            if column.name == "org_id":
                values["org_id"] = org_id
            elif _is_required(column):
                values[column.name] = await self._required_value(session, table, column, org_id)
        return values

    async def insert(self, session: AsyncSession, table: Table, org_id: uuid.UUID | None) -> RowKey:
        """Insert a minimal row of ``table`` owned by ``org_id`` and return its primary key."""
        values = await self.values(session, table, org_id)
        return await self.insert_values(session, table, values)

    async def insert_values(self, session: AsyncSession, table: Table, values: dict[str, Any]) -> RowKey:
        """Insert ``values`` into ``table`` and return the primary key of the new row."""
        statement = insert(table).values(**values).returning(*table.primary_key.columns)
        row = (await session.execute(statement)).one()
        return dict(row._mapping)

    async def _required_value(
        self, session: AsyncSession, table: Table, column: Column[Any], org_id: uuid.UUID | None
    ) -> Any:
        foreign_keys = list(column.foreign_keys)
        if not foreign_keys:
            return column_value(table, column)
        target = foreign_keys[0].column
        if target.table.name == "orgs" and org_id is not None:
            return org_id
        parent_key = await self.insert(session, target.table, org_id)
        return parent_key[target.name]
