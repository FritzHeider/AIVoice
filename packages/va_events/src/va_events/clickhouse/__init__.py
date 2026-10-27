"""ClickHouse analytics schema (specs/03-data-model.md §12): packaged migrations, applier, and client."""

from __future__ import annotations

from va_events.clickhouse.client import ClickHouseTarget, open_clickhouse, parse_clickhouse_url
from va_events.clickhouse.schema import (
    ClickHouseCommands,
    ClickHouseSchemaDriftError,
    Migration,
    apply_migrations,
    load_migrations,
)

__all__ = [
    "ClickHouseCommands",
    "ClickHouseSchemaDriftError",
    "ClickHouseTarget",
    "Migration",
    "apply_migrations",
    "load_migrations",
    "open_clickhouse",
    "parse_clickhouse_url",
]
