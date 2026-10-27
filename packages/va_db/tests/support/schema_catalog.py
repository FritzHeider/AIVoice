"""Read a Postgres catalog back as comparable sets, apply the specs/03 SQL blocks, run Alembic autogenerate.

The schema-conformance test builds one database from the spec text and one from the migration and compares
their snapshots; the autogenerate-parity test compares the models with the migrated database.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from typing import Any, Final

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from migration_helpers import load_initial_revision, query_many
from model_introspection import spec_sql_blocks
from service_helpers import settings_of
from sqlalchemy import Connection, MetaData

from va_common.settings import DatabaseSettings
from va_db.engine import create_engine

__all__ = [
    "PARTITION_NAME",
    "TABLES_SQL",
    "CatalogSnapshot",
    "apply_spec",
    "autogenerate_diff",
    "diff",
    "include_object",
    "restrict",
    "snapshot",
]

TABLES_SQL: Final = """
SELECT c.relname FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') AND NOT c.relispartition
"""
# Every table-scoped query below joins to this set: base and partitioned tables of schema public, without the
# call_events_pYYYYMM partitions (the spec creates none; their indexes and triggers are clones) and
# alembic_version.
_RELATIONS: Final = """
WITH rel AS (
  SELECT c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c
  WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') AND NOT c.relispartition
    AND c.relname <> 'alembic_version'
)
"""
_COLUMNS: Final = (
    _RELATIONS
    + """
SELECT rel.relname, a.attname, a.attnum, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid), a.attgenerated::text, a.attidentity::text
FROM rel JOIN pg_attribute a ON a.attrelid = rel.oid
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attnum > 0 AND NOT a.attisdropped
"""
)
# conparentid <> 0 marks constraints cloned for partitions, which exist only in the migrated database.
_CONSTRAINTS: Final = (
    _RELATIONS
    + """
SELECT rel.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid, true)
FROM rel JOIN pg_constraint con ON con.conrelid = rel.oid
WHERE con.conparentid = 0
"""
)
_INDEXES: Final = (
    _RELATIONS
    + """
SELECT i.tablename, i.indexname, i.indexdef
FROM rel JOIN pg_indexes i ON i.schemaname = 'public' AND i.tablename = rel.relname
"""
)
_TRIGGERS: Final = (
    _RELATIONS
    + """
SELECT rel.relname, t.tgname, pg_get_triggerdef(t.oid, true)
FROM rel JOIN pg_trigger t ON t.tgrelid = rel.oid
WHERE NOT t.tgisinternal
"""
)
_POLICIES: Final = (
    _RELATIONS
    + """
SELECT p.tablename, p.policyname, p.permissive, p.roles::text, p.cmd, p.qual, p.with_check
FROM rel JOIN pg_policies p ON p.schemaname = 'public' AND p.tablename = rel.relname
"""
)
_RLS: Final = (
    _RELATIONS
    + """
SELECT relname, relrowsecurity, relforcerowsecurity FROM rel
"""
)
_EXTENSIONS: Final = "SELECT extname FROM pg_extension"
# Functions of schema public that no extension owns (pgvector, pg_trgm, and citext install theirs there).
_FUNCTIONS: Final = """
SELECT p.proname, pg_get_functiondef(p.oid) FROM pg_proc p
WHERE p.pronamespace = 'public'::regnamespace
  AND NOT EXISTS (
    SELECT FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
  )
"""

PARTITION_NAME: Final = re.compile(r"^call_events_p\d{6}$")


@dataclass(frozen=True)
class CatalogSnapshot:
    """One database's schema objects as sets of rows; every table-scoped row starts with its table name."""

    columns: frozenset[tuple[Any, ...]]
    constraints: frozenset[tuple[Any, ...]]
    indexes: frozenset[tuple[Any, ...]]
    triggers: frozenset[tuple[Any, ...]]
    policies: frozenset[tuple[Any, ...]]
    rls: frozenset[tuple[Any, ...]]
    extensions: frozenset[tuple[Any, ...]]
    functions: frozenset[tuple[Any, ...]]

    @property
    def tables(self) -> frozenset[str]:
        return frozenset(row[0] for row in self.rls)


_SNAPSHOT_SQL: Final = (_COLUMNS, _CONSTRAINTS, _INDEXES, _TRIGGERS, _POLICIES, _RLS, _EXTENSIONS, _FUNCTIONS)
_TABLE_SCOPED: Final = ("columns", "constraints", "indexes", "triggers", "policies", "rls")


def snapshot(url: str) -> CatalogSnapshot:
    """Read every :class:`CatalogSnapshot` field from the database at ``url`` over one connection."""
    rows = query_many(url, _SNAPSHOT_SQL)
    return CatalogSnapshot(*(frozenset(found) for found in rows))


def restrict(catalog: CatalogSnapshot, tables: Iterable[str]) -> CatalogSnapshot:
    """``catalog`` with the table-scoped fields limited to ``tables``; extensions and functions are kept."""
    keep = frozenset(tables)
    return replace(
        catalog,
        **{
            name: frozenset(row for row in getattr(catalog, name) if row[0] in keep) for name in _TABLE_SCOPED
        },
    )


def diff(
    left: CatalogSnapshot, right: CatalogSnapshot
) -> dict[str, tuple[frozenset[tuple[Any, ...]], frozenset[tuple[Any, ...]]]]:
    """Per field, ``(rows only in left, rows only in right)``; equal fields are omitted."""
    found = {}
    for field in fields(CatalogSnapshot):
        a, b = getattr(left, field.name), getattr(right, field.name)
        if a != b:
            found[field.name] = (a - b, b - a)
    return found


async def _apply_spec(url: str) -> None:
    statements = load_initial_revision().statements
    engine = create_engine(settings_of(DatabaseSettings, database_url=url, database_pool_size=1), None)
    try:
        async with engine.begin() as connection:
            for block in spec_sql_blocks():
                for statement in statements(block):
                    # Driver SQL skips text() bind parsing: `'{}'::jsonb`, JSON colons, `:=` run as written.
                    await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


def apply_spec(url: str) -> None:
    """Run each relational specs/03 SQL block (§2-§10) as written, one statement at a time, in one txn."""
    asyncio.run(_apply_spec(url))


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """Alembic ``include_object`` hook: skip only ``alembic_version`` and ``call_events_pYYYYMM`` partitions.

    RLS, policies, and triggers are invisible to autogenerate, so nothing else needs excluding.
    """
    if type_ == "table" and name is not None:
        return name != "alembic_version" and PARTITION_NAME.match(name) is None
    return True


def _compare(connection: Connection, metadata: MetaData) -> list[Any]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "compare_server_default": True, "include_object": include_object},
    )
    return list(compare_metadata(context, metadata))


async def _autogenerate_diff(url: str, metadata: MetaData) -> list[Any]:
    engine = create_engine(settings_of(DatabaseSettings, database_url=url, database_pool_size=1), None)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_compare, metadata)
    finally:
        await engine.dispose()


def autogenerate_diff(url: str, metadata: MetaData) -> list[Any]:
    """Alembic ``compare_metadata`` of ``metadata`` against the database at ``url`` (empty when in parity)."""
    return asyncio.run(_autogenerate_diff(url, metadata))
