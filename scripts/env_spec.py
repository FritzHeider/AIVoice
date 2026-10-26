"""Parsers for the environment-variable contract in specs/02 §6 and `.env.example`.

`specs/02-repo-layout-and-conventions.md` §6 is the normative list of every environment variable the platform
reads, and §1 requires `.env.example` to mirror it with dev values. This module parses both sides so
`scripts/tests/test_env_example.py` can fail whenever the two drift apart.

It is a library: importing it has no side effects and it has no command-line interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

SECTION_HEADING: Final[str] = (
    "## 6. Environment variables (complete list; mirror in `.env.example` with dev values)"
)
"""The §6 heading that introduces the environment-variable table."""

_LITERAL_RE: Final[re.Pattern[str]] = re.compile(r"`([^`]*)`")
_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"^[|\-: ]+$")
_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_EXPECTED_CELLS: Final[int] = 4
_HEADER_ROWS: Final[int] = 1


class EnvSpecError(ValueError):
    """Raised when the §6 table or `.env.example` does not follow the documented shape."""


class DevValueKind(StrEnum):
    """How a §6 "Dev value" cell constrains `.env.example`."""

    LITERAL = "literal"
    """The cell names an exact value in backticks; `.env.example` must use it verbatim."""

    EMPTY = "empty"
    """The cell says `empty`; `.env.example` must assign an empty value."""

    FREE = "free"
    """The cell describes the value in prose; `.env.example` only has to define the variable."""


@dataclass(frozen=True, slots=True)
class SpecVar:
    """One environment variable declared by a row of the §6 table."""

    name: str
    used_by: str
    dev_cell: str
    kind: DevValueKind
    literal: str | None
    meaning: str
    templated: bool


@dataclass(frozen=True, slots=True)
class EnvAssignment:
    """One `NAME=value  # comment` line of `.env.example`."""

    name: str
    value: str
    comment: str
    line_no: int


def repo_root() -> Path:
    """Return the repository root (the parent of `scripts/`), independent of the current directory."""
    return Path(__file__).resolve().parent.parent


SPEC_PATH: Final[Path] = repo_root() / "specs" / "02-repo-layout-and-conventions.md"
ENV_EXAMPLE_PATH: Final[Path] = repo_root() / ".env.example"


def _table_rows(markdown: str) -> list[str]:
    """Return the body rows of the first table after `SECTION_HEADING`, without header or separator."""
    lines = markdown.splitlines()
    try:
        start = lines.index(SECTION_HEADING) + 1
    except ValueError as exc:
        msg = f"{SECTION_HEADING!r} not found"
        raise EnvSpecError(msg) from exc

    rows: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("|"):
            rows.append(stripped)
        elif rows:
            break
        elif stripped:
            msg = f"no table directly under the §6 heading, found {stripped!r}"
            raise EnvSpecError(msg)

    body = [row for row in rows if not _SEPARATOR_RE.match(row)]
    if len(body) <= _HEADER_ROWS:
        msg = "the §6 table has no data rows"
        raise EnvSpecError(msg)
    return body[_HEADER_ROWS:]


def _cells(row: str) -> list[str]:
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    if len(cells) != _EXPECTED_CELLS:
        msg = f"expected {_EXPECTED_CELLS} cells, got {len(cells)} in {row!r}"
        raise EnvSpecError(msg)
    return cells


def classify_dev_value(cell: str) -> tuple[DevValueKind, str | None]:
    """Classify a §6 "Dev value" cell and extract its literal value when it has one."""
    if cell.startswith("`"):
        match = _LITERAL_RE.match(cell)
        if match is None:
            msg = f"unterminated backtick in dev value {cell!r}"
            raise EnvSpecError(msg)
        return DevValueKind.LITERAL, match.group(1)
    if cell == "empty" or cell.startswith("empty "):
        return DevValueKind.EMPTY, None
    return DevValueKind.FREE, None


def parse_spec_table(markdown: str) -> list[SpecVar]:
    """Parse the §6 table into one `SpecVar` per declared variable, in table order.

    A row may declare two variables as ```A` / `B```; its dev-value cell is then split on `/` positionally.
    """
    variables: list[SpecVar] = []
    for row in _table_rows(markdown):
        name_cell, used_by, dev_cell, meaning = _cells(row)
        names = [part.strip().strip("`").strip() for part in name_cell.split("/")]
        dev_cells = [dev_cell]
        if len(names) > 1:
            dev_cells = [part.strip() for part in dev_cell.split("/")]
            if len(dev_cells) != len(names):
                msg = f"row {name_cell!r} declares {len(names)} names but {len(dev_cells)} values"
                raise EnvSpecError(msg)
        for name, cell in zip(names, dev_cells, strict=True):
            if not name:
                msg = f"empty variable name in {row!r}"
                raise EnvSpecError(msg)
            kind, literal = classify_dev_value(cell)
            variables.append(
                SpecVar(
                    name=name,
                    used_by=used_by,
                    dev_cell=cell,
                    kind=kind,
                    literal=literal,
                    meaning=meaning,
                    templated="<" in name,
                )
            )
    return variables


def split_trailing_comment(rhs: str) -> tuple[str, str]:
    """Split the right-hand side of an assignment into its value and its trailing comment.

    One layer of surrounding single or double quotes is removed from the value, so a quoted value may contain
    `#` without it being read as a comment. The comment is returned without its leading `#`.
    """
    text = rhs.strip()
    if text[:1] in {'"', "'"}:
        quote = text[0]
        end = text.find(quote, 1)
        if end < 0:
            msg = f"unterminated quote in {rhs!r}"
            raise EnvSpecError(msg)
        value = text[1:end]
        rest = text[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            msg = f"unexpected text after the quoted value in {rhs!r}"
            raise EnvSpecError(msg)
        return value, rest.lstrip("#").strip()
    value, _, comment = text.partition("#")
    return value.strip(), comment.strip()


def parse_env_example(text: str) -> list[EnvAssignment]:
    """Parse the assignment lines of an `.env.example` file, ignoring blank and comment-only lines."""
    assignments: list[EnvAssignment] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT_RE.match(stripped)
        if match is None:
            msg = f"line {line_no} is neither a comment nor an assignment: {stripped!r}"
            raise EnvSpecError(msg)
        value, comment = split_trailing_comment(match.group(2))
        assignments.append(EnvAssignment(name=match.group(1), value=value, comment=comment, line_no=line_no))
    return assignments


def commented_lines(text: str) -> list[str]:
    """Return the comment-only lines of `text`, without their leading `#`."""
    return [
        stripped.lstrip("#").strip()
        for stripped in (line.strip() for line in text.splitlines())
        if stripped.startswith("#")
    ]


def load_spec_variables() -> list[SpecVar]:
    """Parse the committed specs/02 §6 table."""
    return parse_spec_table(SPEC_PATH.read_text(encoding="utf-8"))


def load_env_example() -> str:
    """Read the committed `.env.example`."""
    return ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
