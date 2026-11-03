"""ClickHouse test support: specs/03-data-model.md §12 DDL parsed from the spec, and a fake async client."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from va_events.clickhouse.schema import RECORD_SQL, split_statements

__all__ = [
    "SPEC_03",
    "FakeClickHouse",
    "FakeInsert",
    "FakeRows",
    "InsertRejecter",
    "normalise",
    "spec_columns",
    "spec_statements",
    "top_level_split",
]

SPEC_03: Final = Path(__file__).resolve().parents[4] / "specs" / "03-data-model.md"
_TABLE_RE: Final = re.compile(r"CREATE TABLE IF NOT EXISTS va\.(\w+) \((.*)\) ENGINE", re.DOTALL)


def normalise(sql: str) -> str:
    """``sql`` with every whitespace run collapsed to one space."""
    return " ".join(sql.split())


def spec_statements() -> tuple[str, ...]:
    """The ```sql statements of specs/03 §12, in spec order."""
    markdown = SPEC_03.read_text(encoding="utf-8")
    section = markdown[markdown.index("\n## 12. ") :]
    blocks = re.findall(r"```sql\n(.*?)```", section, re.DOTALL)
    return tuple(statement for block in blocks for statement in split_statements(block))


def top_level_split(text: str) -> list[str]:
    """``text`` split at commas outside parentheses, each part stripped."""
    parts, depth, current = [], 0, ""
    for char in text:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    return [*parts, current.strip()]


def spec_columns() -> dict[str, list[tuple[str, str]]]:
    """``table -> [(column, type)]`` of every specs/03 §12 table, in declaration order."""
    tables: dict[str, list[tuple[str, str]]] = {}
    for statement in spec_statements():
        match = _TABLE_RE.search(normalise(statement))
        assert match is not None, statement
        definitions = top_level_split(match.group(2))
        tables[match.group(1)] = [(d.split(" ", 1)[0], d.split(" ", 1)[1]) for d in definitions]
    return tables


@dataclass(frozen=True)
class FakeRows:
    result_rows: Sequence[Sequence[Any]]


@dataclass(frozen=True)
class FakeInsert:
    table: str
    rows: list[tuple[Any, ...]]
    column_names: tuple[str, ...]
    database: str | None


type InsertRejecter = Callable[[str, Sequence[Sequence[Any]]], BaseException | None]


@dataclass
class FakeClickHouse:
    """Records commands, queries, and inserts; serves ``schema_migrations`` rows from ``recorded``.

    ``insert`` raises the queued ``insert_failures`` one per call, then whatever ``reject(table, rows)``
    returns (``None`` = accepted, recorded in ``inserts`` and signalled through ``inserted``).
    """

    recorded: dict[str, str] = field(default_factory=dict)
    commands: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)
    queries: list[str | None] = field(default_factory=list)
    inserts: list[FakeInsert] = field(default_factory=list)
    insert_failures: list[BaseException] = field(default_factory=list)
    reject: InsertRejecter | None = None
    attempts: int = 0
    inserted: asyncio.Event = field(default_factory=asyncio.Event)

    # jscpd:ignore-start  (signature of va_events_sink.writer.ClickHouseInserts)
    async def insert(
        self,
        table: str | None = None,
        data: Sequence[Sequence[Any]] | None = None,
        column_names: str | Sequence[str] | None = "*",
        database: str | None = None,
    ) -> object:
        # jscpd:ignore-end  (the signature mirrors va_events_sink.writer.ClickHouseInserts)
        assert table is not None
        rows = [tuple(row) for row in data or []]
        self.attempts += 1
        if self.insert_failures:
            raise self.insert_failures.pop(0)
        if self.reject is not None and (error := self.reject(table, rows)) is not None:
            raise error
        names = (column_names,) if isinstance(column_names, str) else tuple(column_names or ())
        self.inserts.append(FakeInsert(table, rows, names, database))
        self.inserted.set()
        return None

    def rows_of(self, table: str) -> list[tuple[Any, ...]]:
        """Every accepted row of ``table``, in insert order."""
        return [row for insert in self.inserts if insert.table == table for row in insert.rows]

    async def command(self, cmd: str, parameters: dict[str, Any] | None = None) -> object:
        self.commands.append((cmd, parameters))
        if cmd == RECORD_SQL and parameters is not None:
            self.recorded[parameters["version"]] = parameters["checksum"]
        return ""

    async def query(self, query: str | None = None) -> FakeRows:
        self.queries.append(query)
        return FakeRows(sorted(self.recorded.items()))

    @property
    def statements(self) -> list[str]:
        return [cmd for cmd, _ in self.commands]
