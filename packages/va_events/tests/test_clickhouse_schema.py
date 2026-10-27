"""ClickHouse migrations: packaging, spec parity (specs/03-data-model.md §12), splitting, and the applier."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
from ch_support import FakeClickHouse, normalise, spec_statements
from service_helpers import json_log_stream, log_events

from va_common.time import FrozenClock
from va_events.clickhouse import ClickHouseSchemaDriftError, Migration, apply_migrations, load_migrations
from va_events.clickhouse.schema import (
    CREATE_DATABASE_SQL,
    RECORD_SQL,
    RECORDED_SQL,
    SCHEMA_MIGRATIONS_DDL,
    split_statements,
)

CLOCK_START = datetime(2026, 10, 27, 9, 30, 15, 123456, tzinfo=UTC)


def migration(version: str, *statements: str) -> Migration:
    return Migration(version, f"m{version}", statements, checksum=f"sum-{version}")


def test_packaged_migrations_are_the_initial_schema() -> None:
    (initial,) = load_migrations()
    raw = files("va_events.clickhouse").joinpath("migrations/0001_init.sql").read_bytes()
    assert (initial.version, initial.name) == ("0001", "init")
    assert initial.checksum == hashlib.sha256(raw).hexdigest()
    assert initial.statements[0] == CREATE_DATABASE_SQL
    assert len(initial.statements) == 6


def test_initial_migration_tables_equal_the_spec_ddl() -> None:
    (initial,) = load_migrations()
    assert [normalise(s) for s in initial.statements[1:]] == [normalise(s) for s in spec_statements()]
    assert [s.split(" (")[0] for s in spec_statements()] == [
        f"CREATE TABLE IF NOT EXISTS va.{name}"
        for name in ("call_events", "call_turns", "calls_fact", "usage_events_raw", "eval_metrics")
    ]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1; SELECT 2;", ("SELECT 1", "SELECT 2")),
        ("SELECT ';' AS s; SELECT 2", ("SELECT ';' AS s", "SELECT 2")),
        ("SELECT 'it''s; fine'", ("SELECT 'it''s; fine'",)),
        ("SELECT 'a\\'; b'", ("SELECT 'a\\'; b'",)),
        ("-- header; with a separator\nSELECT 1 -- trailing\n;", ("SELECT 1",)),
        ("SELECT '-- not a comment'", ("SELECT '-- not a comment'",)),
        ("SELECT 2 - 1", ("SELECT 2 - 1",)),
        ("  ;\n; -- only a comment\n", ()),
        ("", ()),
    ],
)
def test_split_statements(sql: str, expected: tuple[str, ...]) -> None:
    assert split_statements(sql) == expected


def test_split_statements_rejects_an_unterminated_literal() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        split_statements("SELECT 'oops; SELECT 2")


def test_load_migrations_sorts_by_version_and_ignores_other_files(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 2;")
    (tmp_path / "0001_first.sql").write_text("SELECT 1; SELECT 11;")
    (tmp_path / "README.txt").write_text("not a migration")
    loaded = load_migrations(tmp_path)
    assert [(m.version, m.name, m.statements) for m in loaded] == [
        ("0001", "first", ("SELECT 1", "SELECT 11")),
        ("0002", "second", ("SELECT 2",)),
    ]


@pytest.mark.parametrize("name", ["1_short.sql", "0001-dash.sql", "0001_Upper.sql", "0001_.sql"])
def test_load_migrations_rejects_bad_file_names(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(ValueError, match="invalid ClickHouse migration file name"):
        load_migrations(tmp_path)


def test_load_migrations_rejects_duplicate_versions(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 2;")
    with pytest.raises(ValueError, match="duplicate ClickHouse migration versions: 0001"):
        load_migrations(tmp_path)


async def test_first_apply_runs_bootstrap_statements_and_records_the_version() -> None:
    client = FakeClickHouse()
    (initial,) = load_migrations()
    assert await apply_migrations(client, clock=FrozenClock(CLOCK_START)) == ["0001"]
    assert client.statements == [CREATE_DATABASE_SQL, SCHEMA_MIGRATIONS_DDL, *initial.statements, RECORD_SQL]
    assert client.queries == [RECORDED_SQL]
    assert client.commands[-1][1] == {
        "version": "0001",
        "name": "init",
        "checksum": initial.checksum,
        "applied_at": "2026-10-27 09:30:15.123",
    }


async def test_second_apply_runs_no_migration_statements() -> None:
    client = FakeClickHouse()
    await apply_migrations(client, clock=FrozenClock())
    client.commands.clear()
    assert await apply_migrations(client, clock=FrozenClock()) == []
    assert client.statements == [CREATE_DATABASE_SQL, SCHEMA_MIGRATIONS_DDL]


async def test_changed_applied_migration_raises_drift() -> None:
    client = FakeClickHouse(recorded={"0001": "edited-elsewhere"})
    with pytest.raises(ClickHouseSchemaDriftError, match="clickhouse migration 0001 changed") as raised:
        await apply_migrations(client, clock=FrozenClock())
    assert raised.value.version == "0001"
    assert "edited-elsewhere" in str(raised.value)
    assert client.statements == [CREATE_DATABASE_SQL, SCHEMA_MIGRATIONS_DDL]


async def test_pending_migrations_apply_in_version_order_after_recorded_ones() -> None:
    client = FakeClickHouse(recorded={"0001": "sum-0001"})
    pending = [migration("0003", "C3"), migration("0001", "C1"), migration("0002", "C2a", "C2b")]
    assert await apply_migrations(client, clock=FrozenClock(), migrations=pending) == ["0002", "0003"]
    assert client.statements[2:] == ["C2a", "C2b", RECORD_SQL, "C3", RECORD_SQL]


async def test_applied_migration_is_logged() -> None:
    stream = json_log_stream()
    await apply_migrations(FakeClickHouse(), clock=FrozenClock(), migrations=[migration("0007", "A", "B")])
    (event,) = [e for e in log_events(stream) if e["event"] == "clickhouse.migration_applied"]
    assert (event["migration"], event["name"], event["statements"]) == ("0007", "m0007", 2)
