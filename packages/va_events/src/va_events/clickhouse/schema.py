"""Versioned ClickHouse DDL (specs/03-data-model.md §12) and its idempotent applier.

Migrations are the packaged ``migrations/NNNN_name.sql`` files. The ClickHouse HTTP interface runs one
statement per request, so every file is split at top-level ``;`` and sent statement by statement. Applied
versions are recorded in ``va.schema_migrations`` (specs/03 §12 Decision); a recorded version whose checksum
no longer matches the packaged file raises :class:`ClickHouseSchemaDriftError` (applied migrations are
immutable).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any, Final, Protocol

from va_common.logging import get_logger
from va_common.time import Clock

__all__ = [
    "CREATE_DATABASE_SQL",
    "MIGRATION_FILE_RE",
    "SCHEMA_MIGRATIONS_DDL",
    "ClickHouseCommands",
    "ClickHouseSchemaDriftError",
    "Migration",
    "RowsResult",
    "apply_migrations",
    "load_migrations",
    "split_statements",
]

MIGRATION_FILE_RE: Final = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
CREATE_DATABASE_SQL: Final = "CREATE DATABASE IF NOT EXISTS va"
SCHEMA_MIGRATIONS_DDL: Final = (
    "CREATE TABLE IF NOT EXISTS va.schema_migrations "
    "(version String, name String, checksum String, applied_at DateTime64(3, 'UTC')) "
    "ENGINE = ReplacingMergeTree(applied_at) ORDER BY version"
)
# Concurrent sink pods may record the same version twice; the newest row per version wins.
RECORDED_SQL: Final = (
    "SELECT version, argMax(checksum, applied_at) FROM va.schema_migrations GROUP BY version"
)
RECORD_SQL: Final = (
    "INSERT INTO va.schema_migrations (version, name, checksum, applied_at) "
    "SELECT {version:String}, {name:String}, {checksum:String}, {applied_at:DateTime64(3, 'UTC')}"
)

# A single-quoted literal (backslash and doubled-quote escapes), a line comment, a separator, or plain text.
_TOKEN_RE: Final = re.compile(r"'(?:[^'\\]|\\.|'')*'|--[^\n]*|;|[^';-]+|-|(?P<unterminated>')", re.DOTALL)

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Migration:
    """One packaged migration file: its statements in order and the sha256 of its bytes."""

    version: str
    name: str
    statements: tuple[str, ...]
    checksum: str


class RowsResult(Protocol):
    @property
    def result_rows(self) -> Sequence[Sequence[Any]]: ...


class ClickHouseCommands(Protocol):
    """The part of ``clickhouse_connect`` ``AsyncClient`` the applier uses."""

    async def command(self, cmd: str, parameters: dict[str, Any] | None = None) -> object: ...

    async def query(self, query: str | None = None) -> RowsResult: ...


class ClickHouseSchemaDriftError(RuntimeError):
    """An applied migration was edited after it was recorded."""

    def __init__(self, version: str, *, expected: str, recorded: str) -> None:
        super().__init__(
            f"clickhouse migration {version} changed after it was applied "
            f"(packaged checksum {expected}, recorded {recorded}); add a new migration instead"
        )
        self.version = version


def _statement_parts(sql: str) -> list[list[str]]:
    parts: list[list[str]] = [[]]
    for match in _TOKEN_RE.finditer(sql):
        piece = match.group()
        if match.group("unterminated") is not None:
            msg = "unterminated string literal in ClickHouse migration"
            raise ValueError(msg)
        if piece == ";":
            parts.append([])
        elif not piece.startswith("--"):
            parts[-1].append(piece)
    return parts


def split_statements(sql: str) -> tuple[str, ...]:
    """Statements of ``sql`` split at ``;`` outside string literals, with ``--`` comments dropped."""
    statements = ("".join(part).strip() for part in _statement_parts(sql))
    return tuple(statement for statement in statements if statement)


def _packaged_migrations() -> Traversable:
    return files("va_events.clickhouse").joinpath("migrations")


def _load(entry: Traversable) -> Migration:
    match = MIGRATION_FILE_RE.fullmatch(entry.name)
    if match is None:
        msg = f"invalid ClickHouse migration file name {entry.name!r} (expected NNNN_name.sql)"
        raise ValueError(msg)
    data = entry.read_bytes()
    return Migration(
        version=match.group(1),
        name=match.group(2),
        statements=split_statements(data.decode()),
        checksum=hashlib.sha256(data).hexdigest(),
    )


def _reject_duplicate_versions(migrations: Sequence[Migration]) -> None:
    versions = Counter(migration.version for migration in migrations)
    duplicates = sorted(version for version, count in versions.items() if count > 1)
    if duplicates:
        msg = f"duplicate ClickHouse migration versions: {', '.join(duplicates)}"
        raise ValueError(msg)


def load_migrations(directory: Traversable | None = None) -> tuple[Migration, ...]:
    """Every ``.sql`` migration in ``directory`` (default: the packaged ones), sorted by version."""
    root = _packaged_migrations() if directory is None else directory
    migrations = sorted(
        (_load(entry) for entry in root.iterdir() if entry.name.endswith(".sql")), key=lambda m: m.version
    )
    _reject_duplicate_versions(migrations)
    return tuple(migrations)


async def _recorded_checksums(client: ClickHouseCommands) -> dict[str, str]:
    result = await client.query(RECORDED_SQL)
    return {str(version): str(checksum) for version, checksum in result.result_rows}


def _is_applied(migration: Migration, recorded: Mapping[str, str]) -> bool:
    checksum = recorded.get(migration.version)
    if checksum is None:
        return False
    if checksum != migration.checksum:
        raise ClickHouseSchemaDriftError(migration.version, expected=migration.checksum, recorded=checksum)
    return True


async def _apply(client: ClickHouseCommands, migration: Migration, clock: Clock) -> None:
    for statement in migration.statements:
        await client.command(statement)
    applied_at = clock.now().astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    await client.command(
        RECORD_SQL,
        parameters={
            "version": migration.version,
            "name": migration.name,
            "checksum": migration.checksum,
            "applied_at": applied_at,
        },
    )
    _log.info(
        "clickhouse.migration_applied",
        migration=migration.version,
        name=migration.name,
        statements=len(migration.statements),
    )


async def apply_migrations(
    client: ClickHouseCommands, *, clock: Clock, migrations: Sequence[Migration] | None = None
) -> list[str]:
    """Apply every migration not yet recorded, in version order; return the versions applied by this call."""
    pending = load_migrations() if migrations is None else tuple(migrations)
    await client.command(CREATE_DATABASE_SQL)
    await client.command(SCHEMA_MIGRATIONS_DDL)
    recorded = await _recorded_checksums(client)
    applied: list[str] = []
    for migration in sorted(pending, key=lambda m: m.version):
        if _is_applied(migration, recorded):
            continue
        await _apply(client, migration, clock)
        applied.append(migration.version)
    return applied
