"""``python -m va_db.migrate``: bring a database to the current schema (specs/29 §2 ``migrate`` service).

The command applies every Alembic revision packaged with ``va_db`` (``upgrade head`` through
``va_db/migrations/env.py``, which reads ``DATABASE_URL`` and runs as the login role), then creates the
``call_events`` partitions for the current UTC month and the months ahead in one login-role
transaction (:func:`va_db.partitions.ensure_call_event_partitions`). Both steps are idempotent, so the
compose ``migrate`` job and ``make migrate`` can run it on every start. Exit code 0 on success, 1 when
either step fails.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.resources
from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar, Final

from alembic import command
from alembic.config import Config

from va_common.logging import configure_logging, get_logger
from va_common.settings import DatabaseSettings
from va_common.time import SystemClock
from va_db.engine import create_engine
from va_db.partitions import DEFAULT_MONTHS_AHEAD, ensure_call_event_partitions

__all__ = ["MigrateSettings", "alembic_config", "main", "migrate"]

_log = get_logger(__name__)

HEAD: Final = "head"
DESCRIPTION: Final = "Apply the packaged va_db migrations and create the call_events partitions ahead."


class MigrateSettings(DatabaseSettings):
    """``DATABASE_URL`` plus the logging variables every service reads."""

    service_name: ClassVar[str] = "migrate"


def alembic_config() -> Config:
    """An Alembic configuration pointing at the migrations shipped inside the ``va_db`` package."""
    config = Config()
    config.set_main_option("script_location", str(importlib.resources.files("va_db") / "migrations"))
    config.set_main_option("path_separator", "os")
    return config


async def _ensure_partitions(settings: DatabaseSettings, now: datetime, months_ahead: int) -> list[str]:
    engine = create_engine(settings, None)
    try:
        async with engine.begin() as conn:
            return await ensure_call_event_partitions(conn, now, months_ahead)
    finally:
        await engine.dispose()


def migrate(
    settings: DatabaseSettings, *, now: datetime, months_ahead: int = DEFAULT_MONTHS_AHEAD
) -> list[str]:
    """Upgrade to head, then create the missing partitions; return the names of the partitions created."""
    # env.py reads DATABASE_URL itself and runs its own event loop, so the upgrade precedes asyncio.run.
    command.upgrade(alembic_config(), HEAD)
    return asyncio.run(_ensure_partitions(settings, now, months_ahead))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m va_db.migrate", description=DESCRIPTION)
    parser.add_argument(
        "--months-ahead",
        type=int,
        default=DEFAULT_MONTHS_AHEAD,
        help=f"call_events partitions to keep ahead of the current month (default {DEFAULT_MONTHS_AHEAD})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run :func:`migrate` with settings from the environment; log the outcome and return the exit code."""
    args = _parser().parse_args(argv)
    if args.months_ahead < 0:
        _parser().error("--months-ahead must be zero or positive")
    settings = MigrateSettings()
    configure_logging(settings)
    try:
        created = migrate(settings, now=SystemClock().now(), months_ahead=args.months_ahead)
    except Exception as error:  # any failure is reported through the exit code
        _log.error("db.migrate_failed", error_type=type(error).__name__, error=str(error))
        return 1
    _log.info("db.migrated", revision=HEAD, partitions_created=created)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
