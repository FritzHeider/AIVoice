from __future__ import annotations

import pytest
from service_helpers import settings_of

from va_common.ids import new_id
from va_common.settings import DatabaseSettings
from va_db import session as db_session
from va_db.session import Database, configure_database, get_database


async def test_unconfigured_database_raises() -> None:
    configure_database(None)
    with pytest.raises(RuntimeError, match="configure_database"):
        get_database()
    with pytest.raises(RuntimeError, match="configure_database"):
        async with db_session.tenant_session(new_id()):
            pytest.fail("entered a session without a database")
    with pytest.raises(RuntimeError, match="configure_database"):
        async with db_session.system_session():
            pytest.fail("entered a session without a database")


async def test_from_settings_builds_one_engine_per_pool() -> None:
    database = Database.from_settings(settings_of(DatabaseSettings, database_pool_size=3))
    try:
        assert database.app_engine is not database.system_engine
        for engine in (database.app_engine, database.system_engine):
            assert engine.pool.size() == 3  # type: ignore[attr-defined]
        configure_database(database)
        assert get_database() is database
    finally:
        await database.dispose()
