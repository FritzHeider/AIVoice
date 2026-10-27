"""ClickHouse test support: specs/03-data-model.md §12 DDL parsed from the spec, and a fake async client."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from va_events.clickhouse.schema import RECORD_SQL, split_statements

__all__ = [
    "SPEC_03",
    "FakeClickHouse",
    "FakeRows",
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


@dataclass
class FakeClickHouse:
    """Records commands and queries; serves ``schema_migrations`` rows from ``recorded`` (version -> sum)."""

    recorded: dict[str, str] = field(default_factory=dict)
    commands: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)
    queries: list[str | None] = field(default_factory=list)

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
