"""Run the va_db Alembic migrations against disposable databases and read the catalog back."""

from __future__ import annotations

import asyncio
import importlib.util
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from alembic import command
from alembic.config import Config
from service_helpers import settings_of
from sqlalchemy import make_url, text

from va_common.settings import DatabaseSettings
from va_db.engine import create_engine

__all__ = [
    "ALEMBIC_INI",
    "INITIAL_REVISION",
    "alembic_config",
    "downgrade",
    "fresh_database",
    "load_initial_revision",
    "query",
    "upgrade",
]

VA_DB_ROOT: Final = Path(__file__).resolve().parents[2]
ALEMBIC_INI: Final = VA_DB_ROOT / "alembic.ini"
INITIAL_REVISION: Final = VA_DB_ROOT / "migrations" / "versions" / "0001_initial.py"


def alembic_config(output: StringIO | None = None, *, configure_logger: bool = False) -> Config:
    """The repository ``alembic.ini``; offline SQL goes to ``output``; the test's logging setup is kept."""
    config = Config(str(ALEMBIC_INI), output_buffer=output)
    config.attributes["configure_logger"] = configure_logger
    return config


def load_initial_revision() -> ModuleType:
    """Import ``0001_initial.py`` from its path (revision files are not importable by module name)."""
    spec = importlib.util.spec_from_file_location("va_db_migration_0001_initial", INITIAL_REVISION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade(url: str, revision: str = "head") -> None:
    """``alembic upgrade`` against ``url`` (passed as ``DATABASE_URL``, as ``make migrate`` does)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", url)
        command.upgrade(alembic_config(), revision)


def downgrade(url: str, revision: str = "base") -> None:
    """``alembic downgrade`` against ``url``."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", url)
        command.downgrade(alembic_config(), revision)


async def _query(url: str, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    settings = settings_of(DatabaseSettings, database_url=url, database_pool_size=1)
    engine = create_engine(settings, None)
    try:
        async with engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            result = await connection.execute(text(sql), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


def query(url: str, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    """Run one autocommitted statement as the login role and return its rows (empty for no result set)."""
    return asyncio.run(_query(url, sql, params))


@contextmanager
def fresh_database(server_url: str) -> Iterator[str]:
    """A new empty database on the server of ``server_url``; dropped (forcing disconnects) afterwards."""
    name = f"mig_{secrets.token_hex(6)}"
    query(server_url, f'CREATE DATABASE "{name}"')
    try:
        yield make_url(server_url).set(database=name).render_as_string(hide_password=False)
    finally:
        query(server_url, f'DROP DATABASE "{name}" WITH (FORCE)')
