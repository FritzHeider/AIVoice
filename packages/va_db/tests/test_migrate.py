"""``python -m va_db.migrate``: packaged migrations to head plus ``call_events`` partitions (specs/29 §2)."""

from __future__ import annotations

import importlib.resources
from datetime import UTC, datetime
from typing import Final

import pytest
from alembic.script import ScriptDirectory
from migration_helpers import fresh_database, query
from service_helpers import UNREACHABLE, json_log_stream, log_events, reset_logging, settings_of

from va_common.settings import DatabaseSettings
from va_common.time import utcnow
from va_db import migrate as migrate_module
from va_db.migrate import alembic_config, main, migrate
from va_db.partitions import DEFAULT_MONTHS_AHEAD, planned_call_event_partitions

PARTITIONS_SQL: Final = """
SELECT child.relname FROM pg_inherits
JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
JOIN pg_class child ON child.oid = pg_inherits.inhrelid
WHERE parent.relname = 'call_events'
"""
FUTURE: Final = datetime(2031, 5, 17, 12, 0, tzinfo=UTC)
PARTITION_FAILURE: Final = "partitions failed"

__all__ = ["reset_logging"]  # autouse: main() configures logging


def _head() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def _partitions(url: str) -> set[str]:
    return {row[0] for row in query(url, PARTITIONS_SQL)}


def test_migrations_ship_inside_the_va_db_package() -> None:
    migrations = importlib.resources.files("va_db") / "migrations"
    assert (migrations / "env.py").is_file()
    assert (migrations / "script.py.mako").is_file()
    assert (migrations / "versions" / "0001_initial.py").is_file()
    assert _head() == "0006"


def test_negative_months_ahead_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--months-ahead", "-1"])
    assert raised.value.code == 2


def test_an_unreachable_database_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE["database_url"])
    assert main([]) == 1


@pytest.mark.integration
def test_main_migrates_to_head_and_keeps_partitions_ahead(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fresh_database(pg_url) as url:
        monkeypatch.setenv("DATABASE_URL", url)
        assert main([]) == 0
        assert query(url, "SELECT version_num FROM alembic_version") == [(_head(),)]
        planned = {
            partition.name for partition in planned_call_event_partitions(utcnow(), DEFAULT_MONTHS_AHEAD)
        }
        assert planned <= _partitions(url)
        before = _partitions(url)
        assert main([]) == 0
        assert _partitions(url) == before


@pytest.mark.integration
def test_migrate_creates_missing_partitions_in_month_order(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fresh_database(pg_url) as url:
        monkeypatch.setenv("DATABASE_URL", url)
        settings = settings_of(DatabaseSettings, database_url=url, database_pool_size=1)
        created = migrate(settings, now=FUTURE, months_ahead=2)
        assert created == ["call_events_p203105", "call_events_p203106", "call_events_p203107"]
        assert migrate(settings, now=FUTURE, months_ahead=2) == []


def _keep_test_logging(settings: DatabaseSettings) -> None:
    assert settings.service_name == "migrate"


def test_a_failed_partition_step_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(settings: DatabaseSettings, *, now: datetime, months_ahead: int) -> list[str]:
        raise RuntimeError(PARTITION_FAILURE)

    monkeypatch.setattr(migrate_module, "migrate", failing)
    stream = json_log_stream()
    monkeypatch.setattr(migrate_module, "configure_logging", _keep_test_logging)
    assert main(["--months-ahead", "1"]) == 1
    (event,) = [e for e in log_events(stream) if e["event"] == "db.migrate_failed"]
    assert (event["error_type"], event["error"]) == ("RuntimeError", PARTITION_FAILURE)
