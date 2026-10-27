from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from tenant_rows import open_database

from va_common.ids import new_id
from va_db import session as db_session
from va_db.session import Database, configure_database

pytestmark = pytest.mark.integration

_ORG_ID = text("SELECT nullif(current_setting('app.org_id', true), '')")
_PID = text("SELECT pg_backend_pid()")
_USER = text("SELECT current_user")


class _BoomError(Exception):
    """Raised inside a session to force a rollback."""


@pytest.fixture
async def database(pg_url: str) -> AsyncIterator[Database]:
    async with open_database(pg_url) as db:
        yield db


async def _org_id(session: AsyncSession) -> str | None:
    value: str | None = (await session.execute(_ORG_ID)).scalar_one()
    return value


async def _insert(session: AsyncSession, org_id: uuid.UUID, value: str) -> None:
    await session.execute(
        text("INSERT INTO rls_probe (org_id, v) VALUES (:org_id, :v)"), {"org_id": org_id, "v": value}
    )


async def _values(session: AsyncSession, *org_ids: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    rows = await session.execute(
        text("SELECT org_id, v FROM rls_probe WHERE org_id = ANY(:ids) ORDER BY v"), {"ids": list(org_ids)}
    )
    return [(row.org_id, row.v) for row in rows]


async def test_tenant_session_sets_org_id(database: Database) -> None:
    org = new_id()
    async with database.tenant_session(org) as session:
        assert await _org_id(session) == str(org)


async def test_setting_is_cleared_after_tenant_session(database: Database) -> None:
    org = new_id()
    async with database.tenant_session(org) as session:
        pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    async with database.app_engine.connect() as connection:
        assert (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one() == pid
        assert (await connection.execute(_ORG_ID)).scalar_one() is None


async def test_exception_rolls_back_and_clears_setting(database: Database) -> None:
    org = new_id()
    with pytest.raises(_BoomError):
        async with database.tenant_session(org) as session:
            await _insert(session, org, "lost")
            raise _BoomError
    async with database.app_engine.connect() as connection:
        assert (await connection.execute(_ORG_ID)).scalar_one() is None
    async with database.system_session() as session:
        assert await _values(session, org) == []


async def test_tenant_session_commits_on_success(database: Database) -> None:
    org = new_id()
    async with database.tenant_session(org) as session:
        await _insert(session, org, "kept")
    async with database.system_session() as session:
        assert await _values(session, org) == [(org, "kept")]


async def test_sequential_sessions_do_not_leak_org(database: Database) -> None:
    first, second = new_id(), new_id()
    async with database.tenant_session(first) as session:
        assert await _org_id(session) == str(first)
    async with database.tenant_session(second) as session:
        assert await _org_id(session) == str(second)


async def test_tenant_session_runs_as_va_app(database: Database) -> None:
    async with database.tenant_session(new_id()) as session:
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == "va_app"


async def test_system_session_runs_as_va_system_without_org(database: Database) -> None:
    async with database.system_session() as session:
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == "va_system"
        assert await _org_id(session) is None


async def test_rls_confines_tenant_session_to_its_org(database: Database) -> None:
    org_a, org_b = new_id(), new_id()
    async with database.system_session() as session:
        await _insert(session, org_a, "a")
        await _insert(session, org_b, "b")
    async with database.tenant_session(org_a) as session:
        assert await _values(session, org_a, org_b) == [(org_a, "a")]
    with pytest.raises(DBAPIError, match="row-level security"):
        async with database.tenant_session(org_a) as session:
            await _insert(session, org_b, "forged")
    async with database.system_session() as session:
        assert await _values(session, org_a, org_b) == [(org_a, "a"), (org_b, "b")]


async def test_app_engine_without_org_sees_no_tenant_rows(database: Database) -> None:
    org = new_id()
    async with database.system_session() as session:
        await _insert(session, org, "hidden")
    async with database.app_engine.connect() as connection:
        rows = await connection.execute(text("SELECT count(*) FROM rls_probe WHERE org_id = :o"), {"o": org})
        assert rows.scalar_one() == 0


async def test_module_helpers_use_configured_database(database: Database) -> None:
    configure_database(database)
    assert db_session.get_database() is database
    org = new_id()
    async with db_session.tenant_session(org) as session:
        assert await _org_id(session) == str(org)
    async with db_session.system_session() as session:
        assert (await session.execute(text("SELECT current_user"))).scalar_one() == "va_system"


# Regression: the role must survive a fresh connection whose first transaction rolls back
# (exception in a session, or the pool's reset-on-return ROLLBACK after a bare connect()).


async def _first_use_tenant_session_raises(database: Database) -> int:
    with pytest.raises(_BoomError):
        async with database.tenant_session(new_id()) as session:
            pid: int = (await session.execute(_PID)).scalar_one()
            raise _BoomError
    return pid


async def _first_use_system_session_raises(database: Database) -> int:
    with pytest.raises(_BoomError):
        async with database.system_session() as session:
            pid: int = (await session.execute(_PID)).scalar_one()
            raise _BoomError
    return pid


async def _first_use_bare_connect(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        pid: int = (await connection.execute(_PID)).scalar_one()
    return pid


async def _assert_role_on_same_backend(connection: AsyncConnection, pid: int, role: str) -> None:
    assert (await connection.execute(_PID)).scalar_one() == pid
    assert (await connection.execute(_USER)).scalar_one() == role


_FirstUse = Callable[[Database], Awaitable[int]]


@pytest.mark.parametrize(
    "first_use",
    [_first_use_tenant_session_raises, lambda db: _first_use_bare_connect(db.app_engine)],
    ids=["tenant_session_raises", "bare_connect"],
)
async def test_app_role_survives_rolled_back_first_transaction(
    database: Database, first_use: _FirstUse
) -> None:
    org_a, org_b = new_id(), new_id()
    async with database.system_session() as session:
        await _insert(session, org_b, "other")
    pid = await first_use(database)
    async with database.tenant_session(org_a) as session:
        await _assert_role_on_same_backend(await session.connection(), pid, "va_app")
        assert await _values(session, org_a, org_b) == []


@pytest.mark.parametrize(
    "first_use",
    [_first_use_system_session_raises, lambda db: _first_use_bare_connect(db.system_engine)],
    ids=["system_session_raises", "bare_connect"],
)
async def test_system_role_survives_rolled_back_first_transaction(
    database: Database, first_use: _FirstUse
) -> None:
    pid = await first_use(database)
    async with database.system_engine.connect() as connection:
        await _assert_role_on_same_backend(connection, pid, "va_system")


async def test_reset_role_keeps_the_engine_role(database: Database) -> None:
    async with database.tenant_session(new_id()) as session:
        await session.execute(text("RESET ROLE"))
        assert (await session.execute(_USER)).scalar_one() == "va_app"
