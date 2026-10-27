"""Read model metadata back as plain values so model tests can compare it with specs/03-data-model.md."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final, get_args

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.schema import CreateIndex

import va_db.models  # noqa: F401  (registers every table on Base.metadata)
from va_db.base import Base

__all__ = [
    "DIALECT",
    "SPEC_03",
    "assert_enum_check_mirrors",
    "assert_enum_checks_mirror",
    "assert_org_indexed",
    "check_names",
    "columns",
    "computed_columns",
    "constraint",
    "enum_check_names",
    "enum_check_values",
    "expected_foreign_keys",
    "foreign_keys",
    "get_table",
    "index_ddl",
    "leading_columns",
    "naive_timestamps",
    "org_index_ddl",
    "server_defaults",
    "spec_column_mismatches",
    "spec_sql_blocks",
    "spec_table_names",
    "text_array_columns",
    "unique_and_pk",
]

DIALECT: Final = postgresql.dialect()  # type: ignore[no-untyped-call]

_TIMESTAMPS: Final = frozenset({"created_at", "updated_at"})
SPEC_03: Final = Path(__file__).resolve().parents[4] / "specs" / "03-data-model.md"


def spec_table_names() -> list[str]:
    """Table names created in specs/03 §3-§9a, in spec order (the relational schema; §12 is ClickHouse)."""
    markdown = SPEC_03.read_text(encoding="utf-8")
    relational = markdown[markdown.index("\n## 3. ") : markdown.index("\n## 10. ")]
    return re.findall(r"CREATE TABLE (\w+)", relational)


def spec_sql_blocks() -> list[str]:
    """The relational ```sql blocks of specs/03 §2-§10, in spec order (§12 ClickHouse is not Postgres)."""
    markdown = SPEC_03.read_text(encoding="utf-8")
    relational = markdown[markdown.index("\n## 2. ") : markdown.index("\n## 11. ")]
    return re.findall(r"```sql\n(.*?)```", relational, re.DOTALL)


def get_table(name: str) -> Table:
    return Base.metadata.tables[name]


def columns(name: str) -> list[tuple[str, bool]]:
    """``(column, nullable)`` in declaration order."""
    return [(column.name, bool(column.nullable)) for column in get_table(name).columns]


def constraint(table_name: str, name: str) -> Any:
    matches = [c for c in get_table(table_name).constraints if c.name == name]
    assert len(matches) == 1, f"{table_name}: expected one constraint named {name}"
    return matches[0]


def enum_check_values(table_name: str, column: str) -> tuple[str, ...]:
    """Values of ``<table>_<column>_check``, which must be ``<column> IN ('a', ...)``."""
    check = constraint(table_name, f"{table_name}_{column}_check")
    assert isinstance(check, CheckConstraint)
    sql = str(check.sqltext)
    assert sql.startswith(f"{column} IN ("), sql
    return tuple(re.findall(r"'([^']*)'", sql))


def check_names(tables: Iterable[str]) -> set[str]:
    return {
        str(c.name) for name in tables for c in get_table(name).constraints if isinstance(c, CheckConstraint)
    }


def server_defaults(tables: Iterable[str]) -> dict[tuple[str, str], str]:
    """Compiled server defaults keyed by ``(table, column)``, excluding ``created_at``/``updated_at`` and
    generated columns (see :func:`computed_columns`).
    """
    return {
        (name, column.name): str(column.server_default.arg.compile(dialect=DIALECT))  # type: ignore[attr-defined]
        for name in tables
        for column in get_table(name).columns
        if column.server_default is not None
        and not isinstance(column.server_default, Computed)
        and column.name not in _TIMESTAMPS
    }


def computed_columns(tables: Iterable[str]) -> dict[tuple[str, str], tuple[str, bool | None]]:
    """``GENERATED ALWAYS AS (<sqltext>)`` columns by ``(table, column)``, with their ``persisted`` flag."""
    return {
        (name, column.name): (str(column.computed.sqltext), column.computed.persisted)
        for name in tables
        for column in get_table(name).columns
        if column.computed is not None
    }


def index_ddl(tables: Iterable[str]) -> set[str]:
    """``CREATE INDEX`` statements with whitespace collapsed."""
    return {
        " ".join(str(CreateIndex(index).compile(dialect=DIALECT)).split())
        for name in tables
        for index in get_table(name).indexes
    }


def unique_and_pk(tables: Iterable[str]) -> set[tuple[str, tuple[str, ...]]]:
    """``(constraint name, columns)`` of every UNIQUE and PRIMARY KEY constraint."""
    return {
        (str(c.name), tuple(column.name for column in c.columns))
        for name in tables
        for c in get_table(name).constraints
        if isinstance(c, UniqueConstraint | PrimaryKeyConstraint)
    }


def foreign_keys(tables: Iterable[str]) -> set[tuple[str, str, str, str, str | None]]:
    """``(name, table, column, target, ondelete)`` of every single-column foreign key."""
    found = set()
    for name in tables:
        for c in get_table(name).constraints:
            if isinstance(c, ForeignKeyConstraint):
                (element,) = c.elements
                found.add((str(c.name), name, element.parent.name, element.target_fullname, c.ondelete))
    return found


def naive_timestamps(tables: Iterable[str]) -> list[str]:
    """``table.column`` of every ``DateTime`` column without a time zone (the spec has only timestamptz)."""
    return [
        f"{name}.{column.name}"
        for name in tables
        for column in get_table(name).columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]


def text_array_columns(tables: Iterable[str]) -> set[tuple[str, str]]:
    """``(table, column)`` of every ``text[]`` column."""
    return {
        (name, column.name)
        for name in tables
        for column in get_table(name).columns
        if isinstance(column.type, ARRAY) and type(column.type.item_type) is Text
    }


def leading_columns(name: str) -> set[str]:
    """First column of every index and UNIQUE constraint (the specs/03 §1 ``org_id`` index rule)."""
    table = get_table(name)
    elements: set[Index | UniqueConstraint] = {
        *table.indexes,
        *(c for c in table.constraints if isinstance(c, UniqueConstraint)),
    }
    return {element.columns[0].name for element in elements if len(element.columns) > 0}


def spec_column_mismatches(spec_columns: Mapping[str, str]) -> dict[str, list[tuple[str, bool]]]:
    """Actual ``(column, nullable)`` lists of the tables that differ from their spec string.

    A spec string lists the columns in order, separated by spaces; a trailing ``?`` marks a nullable column.
    """
    mismatches = {}
    for name, spec in spec_columns.items():
        expected = [(word.removesuffix("?"), word.endswith("?")) for word in spec.split()]
        if (actual := columns(name)) != expected:
            mismatches[name] = actual
    return mismatches


def assert_enum_check_mirrors(table: str, column: str, literal: Any, expected: tuple[str, ...]) -> None:
    """``<table>_<column>_check`` lists exactly the ``Literal`` values, in the spec's order."""
    assert enum_check_values(table, column) == get_args(literal) == expected


def assert_org_indexed(table: str, *, nullable: bool = False) -> None:
    """specs/03 §1: ``org_id`` (NOT NULL unless ``nullable``) leads an index or UNIQUE constraint."""
    assert get_table(table).c.org_id.nullable is nullable
    assert "org_id" in leading_columns(table)


def org_index_ddl(tables: Iterable[str]) -> set[str]:
    """``CREATE INDEX <table>_org_idx ON <table> (org_id)`` added by the specs/03 §1 decision."""
    return {f"CREATE INDEX {table}_org_idx ON {table} (org_id)" for table in tables}


def enum_check_names(enum_columns: Iterable[tuple[str, str]]) -> set[str]:
    """``<table>_<column>_check`` for every ``(table, column)`` enumeration."""
    return {f"{table}_{column}_check" for table, column in enum_columns}


def expected_foreign_keys(
    org_tables: Iterable[str], others: Iterable[tuple[str, str, str, str | None]]
) -> set[tuple[str, str, str, str, str | None]]:
    """The :func:`foreign_keys` set for ``org_id`` CASCADE on ``org_tables`` plus ``(table, column, target,
    ondelete)`` ``others``, with conventional ``<table>_<column>_fkey`` names.
    """
    org_fks = {(table, "org_id", "orgs.id", "CASCADE") for table in org_tables}
    return {(f"{t}_{c}_fkey", t, c, target, ondelete) for t, c, target, ondelete in {*org_fks, *others}}


def assert_enum_checks_mirror(enums: Mapping[tuple[str, str], tuple[Any, tuple[str, ...]]]) -> None:
    """:func:`assert_enum_check_mirrors` for every ``(table, column) -> (Literal, spec values)`` entry."""
    for (table, column), (literal, expected) in enums.items():
        assert_enum_check_mirrors(table, column, literal, expected)
