"""Alembic autogenerate finds no difference between the models and the 0001_initial database.

The migration is hand-written SQL, so this guards the other direction: every column type, nullability,
server default, index, and constraint on ``Base.metadata`` matches what the migration built. RLS policies and
triggers are invisible to autogenerate; partitions and ``alembic_version`` are skipped by
:func:`schema_catalog.include_object`. Drift cases mutate a copy of the metadata and prove the comparison
fails.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Final

import pytest
from migration_helpers import query
from schema_catalog import autogenerate_diff, include_object
from sqlalchemy import Column, DefaultClause, Integer, MetaData, Table, Text, text

from va_db.base import Base

pytestmark = pytest.mark.integration


def _metadata_copy() -> MetaData:
    copy = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(copy)
    return copy


def _flatten(found: Iterable[Any]) -> Iterable[tuple[Any, ...]]:
    """Autogenerate ops as tuples; ``modify_*`` ops arrive grouped in a nested list per column."""
    for entry in found:
        if isinstance(entry, list):
            yield from _flatten(entry)
        else:
            yield entry


def _op_tables(found: Iterable[Any]) -> set[tuple[str, str]]:
    """``(op name, table name)`` of every autogenerate op.

    Column ops are ``(op, schema, table, ...)``; index and constraint ops are ``(op, object)`` with
    ``.table``; table ops are ``(op, table)``.
    """
    ops = set()
    for op, *rest in _flatten(found):
        if len(rest) > 1 and isinstance(rest[1], str):
            ops.add((op, rest[1]))
        else:
            target = rest[0]
            ops.add((op, target.name if isinstance(target, Table) else target.table.name))
    return ops


def test_models_match_migrated_schema(migrated_url: str) -> None:
    assert autogenerate_diff(migrated_url, Base.metadata) == []


def test_model_index_names_match_database(migrated_url: str) -> None:
    """Index names per table, including expression indexes whose bodies autogenerate does not compare."""
    rows = query(
        migrated_url,
        "SELECT i.tablename, i.indexname FROM pg_indexes i "
        "WHERE i.schemaname = 'public' AND NOT EXISTS ("
        "  SELECT FROM pg_constraint c WHERE c.conindid = i.indexname::regclass AND c.contype IN ('p', 'u'))",
    )
    database = {(table, name) for table, name in rows if include_object(None, table, "table", True, None)}
    models = {
        (table.name, str(index.name)) for table in Base.metadata.sorted_tables for index in table.indexes
    }
    assert database == models


def _add_column(copy: MetaData) -> None:
    copy.tables["orgs"].append_column(Column("drift", Text))


def _flip_nullability(copy: MetaData) -> None:
    copy.tables["agents"].c.name.nullable = True


def _change_type(copy: MetaData) -> None:
    copy.tables["orgs"].c.name.type = Integer()


def _change_default(copy: MetaData) -> None:
    copy.tables["agents"].c.description.server_default = DefaultClause(text("'xx'"))


def _index_remover(table_name: str, index_name: str) -> Callable[[MetaData], None]:
    def remove(copy: MetaData) -> None:
        table = copy.tables[table_name]
        table.indexes.remove(next(index for index in table.indexes if index.name == index_name))

    return remove


DRIFT_CASES: Final[list[tuple[str, Callable[[MetaData], None], tuple[str, str]]]] = [
    ("column-added", _add_column, ("add_column", "orgs")),
    ("nullability-flipped", _flip_nullability, ("modify_nullable", "agents")),
    ("type-changed", _change_type, ("modify_type", "orgs")),
    ("default-changed", _change_default, ("modify_default", "agents")),
    ("index-removed", _index_remover("calls", "calls_org_created_idx"), ("remove_index", "calls")),
    # coalesce()/lower() expression index: autogenerate compares its expressions too.
    (
        "expression-index-removed",
        _index_remover("voices", "voices_name_scope_uq"),
        ("remove_index", "voices"),
    ),
]


@pytest.mark.parametrize(
    ("mutate", "expected"), [case[1:] for case in DRIFT_CASES], ids=[c[0] for c in DRIFT_CASES]
)
def test_parity_detects_model_drift(
    migrated_url: str, mutate: Callable[[MetaData], None], expected: tuple[str, str]
) -> None:
    copy = _metadata_copy()
    mutate(copy)
    assert expected in _op_tables(autogenerate_diff(migrated_url, copy))
    assert autogenerate_diff(migrated_url, Base.metadata) == []
