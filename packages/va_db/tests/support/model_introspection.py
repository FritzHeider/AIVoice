"""Read model metadata back as plain values so model tests can compare it with specs/03-data-model.md."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Final

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKeyConstraint,
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
    "check_names",
    "columns",
    "computed_columns",
    "constraint",
    "enum_check_values",
    "foreign_keys",
    "get_table",
    "index_ddl",
    "naive_timestamps",
    "server_defaults",
    "text_array_columns",
    "unique_and_pk",
]

DIALECT: Final = postgresql.dialect()  # type: ignore[no-untyped-call]

_TIMESTAMPS: Final = frozenset({"created_at", "updated_at"})


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
