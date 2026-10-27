"""Alembic environment for the va_db schema (specs/03-data-model.md).

Migrations run as the ``DATABASE_URL`` login role (specs/03 §2 Decision): online through an asyncpg engine
without an assumed role, or offline (``alembic upgrade head --sql``) as a SQL script. Each revision runs in
its own transaction. ``target_metadata`` is the model metadata, so autogenerate can compare it with a migrated
database.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import va_db.models  # noqa: F401  (registers every table on Base.metadata)
from va_common.settings import DatabaseSettings
from va_db.base import Base
from va_db.engine import create_engine

target_metadata = Base.metadata


def run_migrations_offline(url: str) -> None:
    """Write the migration SQL to the configured output instead of executing it."""
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_on_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online(settings: DatabaseSettings) -> None:
    """Apply the migrations through a login-role engine and dispose of it afterwards."""
    engine = create_engine(settings, None)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_on_connection)
    finally:
        await engine.dispose()


def main() -> None:
    """Entry point Alembic runs for every command; tests set ``configure_logger`` to keep their logging."""
    config = context.config
    if config.config_file_name is not None and config.attributes.get("configure_logger", True):
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    settings = DatabaseSettings()
    if context.is_offline_mode():
        run_migrations_offline(settings.database_url)
    else:
        asyncio.run(run_migrations_online(settings))


main()
