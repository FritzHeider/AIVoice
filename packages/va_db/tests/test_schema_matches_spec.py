"""Schema conformance: the specs/03 SQL blocks applied as written produce the same catalog as 0001_initial.

One database runs every relational ```sql block of specs/03-data-model.md (§2-§10) verbatim; another
runs ``alembic upgrade head``. Columns, constraints, indexes, extensions, and the trigger function must be
identical. The §10 blocks show the trigger and RLS objects for ``calls`` (and ``platform_read`` for
voices/dnc_entries); the migration generates the rest in loops (covered by test_migrations_integration),
so those objects are compared on the §10 tables only.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Final

import pytest
from migration_helpers import fresh_database
from model_introspection import spec_table_names
from schema_catalog import CatalogSnapshot, apply_spec, diff, restrict, snapshot

from va_db.base import Base

pytestmark = pytest.mark.integration

SECTION_10_TABLES: Final = ("calls", "voices", "dnc_entries")


@pytest.fixture(scope="module")
def spec_catalog(pg_url: str) -> Iterator[CatalogSnapshot]:
    with fresh_database(pg_url) as url:
        apply_spec(url)
        yield snapshot(url)


@pytest.fixture(scope="module")
def migrated_catalog(migrated_url: str) -> CatalogSnapshot:
    return snapshot(migrated_url)


def _field_diff(spec: CatalogSnapshot, migrated: CatalogSnapshot, field: str) -> object:
    return diff(spec, migrated).get(field)


def test_spec_tables_equal_model_tables(
    spec_catalog: CatalogSnapshot, migrated_catalog: CatalogSnapshot
) -> None:
    assert len(spec_table_names()) == 72
    assert spec_catalog.tables == set(Base.metadata.tables) == set(spec_table_names())
    assert migrated_catalog.tables == spec_catalog.tables


@pytest.mark.parametrize("field", ["columns", "constraints", "indexes"])
def test_table_objects_match_spec(
    spec_catalog: CatalogSnapshot, migrated_catalog: CatalogSnapshot, field: str
) -> None:
    assert getattr(spec_catalog, field), field
    assert _field_diff(spec_catalog, migrated_catalog, field) is None


def test_column_types_are_exact(migrated_catalog: CatalogSnapshot) -> None:
    types = {(row[0], row[1]): row[3] for row in migrated_catalog.columns}
    assert types[("kb_chunks", "embedding")] == "vector(1024)"
    assert types[("kb_chunks", "tsv")] == "tsvector"
    generated = {(row[0], row[1]) for row in migrated_catalog.columns if row[6] == "s"}
    assert ("kb_chunks", "tsv") in generated


def test_extensions_and_function_match_spec(
    spec_catalog: CatalogSnapshot, migrated_catalog: CatalogSnapshot
) -> None:
    assert {row[0] for row in spec_catalog.extensions} >= {"vector", "pg_trgm", "citext"}
    assert spec_catalog.extensions == migrated_catalog.extensions
    assert {row[0] for row in spec_catalog.functions} == {"set_updated_at"}
    assert spec_catalog.functions == migrated_catalog.functions


def test_section_10_examples_match_migration(
    spec_catalog: CatalogSnapshot, migrated_catalog: CatalogSnapshot
) -> None:
    spec_objects = {name: getattr(spec_catalog, name) for name in ("triggers", "policies")}
    assert {row[:2] for row in spec_objects["triggers"]} == {("calls", "calls_set_updated_at")}
    assert {row[:2] for row in spec_objects["policies"]} == {
        ("calls", "tenant_isolation"),
        ("voices", "platform_read"),
        ("dnc_entries", "platform_read"),
    }
    assert {row[0] for row in spec_catalog.rls if row[1] or row[2]} == {"calls"}
    migrated = restrict(migrated_catalog, ["calls"])
    assert spec_objects["triggers"] == migrated.triggers
    assert restrict(spec_catalog, ["calls"]).rls == migrated.rls
    platform = {
        row for row in restrict(migrated_catalog, SECTION_10_TABLES).policies if row[1] == "platform_read"
    }
    assert spec_objects["policies"] == {*migrated.policies, *platform}
