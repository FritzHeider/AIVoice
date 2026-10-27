"""Migrations to head on real Postgres: tables, triggers, RLS, grants, partitions, roles (specs/03 §2-§10)."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from migration_helpers import downgrade, fresh_database, query, upgrade
from schema_catalog import TABLES_SQL
from service_helpers import settings_of
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from va_common.settings import DatabaseSettings
from va_db.base import Base
from va_db.session import Database

pytestmark = pytest.mark.integration

SPEC_EXTENSIONS: Final = frozenset({"vector", "pg_trgm", "citext"})
PLATFORM_READ_TABLES: Final = frozenset({"voices", "dnc_entries"})
INSERT_ORG: Final = """
INSERT INTO orgs (id, name, slug, webhook_signing_secret_enc, created_at, updated_at)
VALUES (:id, 'Org', :slug, '\\x00'::bytea, '2000-01-01T00:00:00Z', '2000-01-01T00:00:00Z')
"""
INSERT_CALL_EVENT: Final = """
INSERT INTO call_events (id, org_id, call_id, seq, category, message, created_at)
VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 1, 'call', 'routed', :at)
RETURNING tableoid::regclass::text
"""


def _names(url: str, sql: str, **params: object) -> set[str]:
    return {row[0] for row in query(url, sql, **params)}


def _tables_with(column: str) -> set[str]:
    return {table.name for table in Base.metadata.tables.values() if column in table.c}


def _tenant_tables() -> set[str]:
    """specs/03 §10: a NOT NULL org_id, or voices/dnc_entries (whose NULL org_id marks platform rows)."""
    return {
        table.name
        for table in Base.metadata.tables.values()
        if "org_id" in table.c and (not table.c.org_id.nullable or table.name in PLATFORM_READ_TABLES)
    }


def _insert_org(url: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    query(url, INSERT_ORG, id=org_id, slug=f"org-{org_id.hex}")
    return org_id


def test_upgrade_creates_every_model_table(migrated_url: str) -> None:
    assert _names(migrated_url, TABLES_SQL) == {*Base.metadata.tables, "alembic_version"}
    assert _names(migrated_url, "SELECT extname FROM pg_extension") >= SPEC_EXTENSIONS
    assert _names(migrated_url, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'") == {
        "set_updated_at"
    }
    assert query(migrated_url, "SELECT version_num FROM alembic_version")[0][0] == "0002"


def test_every_model_index_exists(migrated_url: str) -> None:
    model_indexes = {index.name for table in Base.metadata.tables.values() for index in table.indexes}
    assert model_indexes <= _names(
        migrated_url, "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
    )
    methods = dict(
        query(
            migrated_url,
            "SELECT i.relname, am.amname FROM pg_class i JOIN pg_am am ON am.oid = i.relam "
            "WHERE i.relname IN ('kb_chunks_embedding_hnsw', 'kb_chunks_tsv_idx')",
        )
    )
    assert methods == {"kb_chunks_embedding_hnsw": "hnsw", "kb_chunks_tsv_idx": "gin"}


def test_updated_at_triggers_cover_exactly_the_updated_at_tables(migrated_url: str) -> None:
    rows = query(
        migrated_url,
        "SELECT t.tgname, c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE NOT t.tgisinternal AND t.tgparentid = 0",
    )
    assert set(rows) == {(f"{table}_set_updated_at", table) for table in _tables_with("updated_at")}


def test_updated_at_trigger_advances_the_timestamp(migrated_url: str) -> None:
    org_id = _insert_org(migrated_url)
    query(migrated_url, "UPDATE orgs SET name = 'Renamed' WHERE id = :id", id=org_id)
    ((created_at, updated_at),) = query(
        migrated_url, "SELECT created_at, updated_at FROM orgs WHERE id = :id", id=org_id
    )
    assert created_at == datetime(2000, 1, 1, tzinfo=UTC)
    assert updated_at > created_at + timedelta(days=365)


def test_rls_enabled_and_forced_on_exactly_the_tenant_tables(migrated_url: str) -> None:
    rows = query(
        migrated_url,
        "SELECT relname, relforcerowsecurity FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relrowsecurity AND NOT relispartition",
    )
    assert {name for name, _ in rows} == _tenant_tables()
    assert all(forced for _, forced in rows)
    assert {"calls", "call_events", "voices", "dnc_entries"} <= _tenant_tables()
    assert {"plans", "telnyx_events", "orgs", "users"}.isdisjoint(_tenant_tables())


def test_tenant_isolation_policies_match_the_spec_expression(migrated_url: str, pg_url: str) -> None:
    # rls_probe (pg_container.SETUP_SQL) has the exact specs/03 §10 policy text; compare deparsed forms.
    expected = set(
        query(pg_url, "SELECT cmd, qual, with_check FROM pg_policies WHERE tablename = 'rls_probe'")
    )
    rows = query(
        migrated_url,
        "SELECT tablename, cmd, qual, with_check FROM pg_policies WHERE policyname = 'tenant_isolation'",
    )
    assert {row[0] for row in rows} == _tenant_tables()
    assert {(cmd, qual, with_check) for _, cmd, qual, with_check in rows} == expected


def test_platform_read_policies_only_on_voices_and_dnc_entries(migrated_url: str) -> None:
    rows = query(
        migrated_url,
        "SELECT tablename, cmd, qual, with_check FROM pg_policies WHERE policyname = 'platform_read'",
    )
    assert set(rows) == {(table, "SELECT", "(org_id IS NULL)", None) for table in PLATFORM_READ_TABLES}


async def _visible_contact_orgs(url: str, org_id: uuid.UUID) -> set[uuid.UUID]:
    database = Database.from_settings(settings_of(DatabaseSettings, database_url=url, database_pool_size=1))
    try:
        async with database.tenant_session(org_id) as session:
            return set((await session.execute(text("SELECT org_id FROM contacts"))).scalars())
    finally:
        await database.dispose()


def test_tenant_session_sees_only_its_own_org(migrated_url: str) -> None:
    org_a, org_b = _insert_org(migrated_url), _insert_org(migrated_url)
    for org_id in (org_a, org_b):
        query(
            migrated_url,
            "INSERT INTO contacts (id, org_id) VALUES (gen_random_uuid(), :org_id)",
            org_id=org_id,
        )
    assert asyncio.run(_visible_contact_orgs(migrated_url, org_a)) == {org_a}
    assert asyncio.run(_visible_contact_orgs(migrated_url, org_b)) == {org_b}


def test_both_roles_hold_every_table_privilege(migrated_url: str) -> None:
    missing = query(
        migrated_url,
        f"SELECT r, t, p FROM unnest(ARRAY['va_app', 'va_system']) r, ({TABLES_SQL}) AS tables(t), "  # noqa: S608  (constant catalog query)
        "unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p "
        "WHERE t <> 'alembic_version' AND NOT has_table_privilege(r, t, p)",
    )
    assert missing == []
    ((usage, version_select),) = query(
        migrated_url,
        "SELECT has_schema_privilege('va_app', 'public', 'USAGE'), "
        "has_table_privilege('va_app', 'alembic_version', 'SELECT')",
    )
    assert (usage, version_select) == (True, False)


def _month_start(moment: datetime, offset: int) -> datetime:
    index = moment.year * 12 + moment.month - 1 + offset
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


def test_call_event_partitions_cover_the_current_utc_month_and_three_more(migrated_url: str) -> None:
    ((now,),) = query(migrated_url, "SELECT now()")
    months = [_month_start(now.astimezone(UTC), offset) for offset in range(5)]
    expected = [f"call_events_p{month:%Y%m}" for month in months[:4]]
    partitions = query(
        migrated_url,
        "SELECT inhrelid::regclass::text FROM pg_inherits "
        "WHERE inhparent = 'call_events'::regclass ORDER BY 1",
    )
    assert [row[0] for row in partitions] == expected
    for name, start, end in zip(expected, months, months[1:], strict=False):
        for moment in (start, end - timedelta(microseconds=1)):
            assert query(migrated_url, INSERT_CALL_EVENT, at=moment) == [(name,)]
    for outside in (months[0] - timedelta(microseconds=1), months[4]):
        with pytest.raises(DBAPIError, match="no partition"):
            query(migrated_url, INSERT_CALL_EVENT, at=outside)


def test_call_event_indexes_attach_to_every_partition(migrated_url: str) -> None:
    rows = query(
        migrated_url,
        "SELECT inhparent::regclass::text, count(*) FROM pg_inherits "
        "WHERE inhparent IN ('call_events_org_idx'::regclass, 'call_events_call_idx'::regclass) GROUP BY 1",
    )
    assert dict(rows) == {"call_events_org_idx": 4, "call_events_call_idx": 4}


def test_downgrade_then_upgrade_on_a_fresh_database(pg_url: str) -> None:
    with fresh_database(pg_url) as url:
        upgrade(url)
        downgrade(url, "0001")
        assert query(url, "SELECT count(*) FROM plans") == [(0,)]
        assert _names(url, TABLES_SQL) == {*Base.metadata.tables, "alembic_version"}
        downgrade(url)
        assert _names(url, TABLES_SQL) == {"alembic_version"}
        assert query(url, "SELECT version_num FROM alembic_version") == []
        assert _names(url, "SELECT proname FROM pg_proc WHERE proname = 'set_updated_at'") == set()
        assert _names(url, "SELECT extname FROM pg_extension").isdisjoint(SPEC_EXTENSIONS)
        upgrade(url)
        assert _names(url, TABLES_SQL) == {*Base.metadata.tables, "alembic_version"}
        assert query(url, "SELECT count(*) FROM plans") == [(4,)]


SPEC_ROLES: Final = ("va_app", "va_system")
ROLE_STATE_SQL: Final = """
SELECT a.rolname, a.rolbypassrls, a.rolcanlogin, a.rolpassword IS NULL,
       EXISTS (SELECT FROM pg_auth_members m WHERE m.roleid = a.oid AND m.member = current_user::regrole)
FROM pg_authid a WHERE a.rolname IN ('va_app', 'va_system') ORDER BY 1
"""


@contextmanager
def spec_roles_set_aside(server_url: str) -> Iterator[None]:
    """Rename the server's va_app/va_system away for the block, then drop the replacements and rename back.

    Grants and memberships follow the role OID, so databases created earlier keep theirs; xdist workers each
    have their own server, and tests in one worker run sequentially.
    """
    suffix = secrets.token_hex(4)
    for role in SPEC_ROLES:
        query(server_url, f"ALTER ROLE {role} RENAME TO {role}_{suffix}")
    try:
        yield
    finally:
        for role in SPEC_ROLES:
            query(server_url, f"DROP ROLE IF EXISTS {role}")
            query(server_url, f"ALTER ROLE {role}_{suffix} RENAME TO {role}")


def test_roles_are_created_when_absent_and_bypassrls_is_restored(pg_url: str) -> None:
    with spec_roles_set_aside(pg_url), fresh_database(pg_url) as url:
        assert query(url, ROLE_STATE_SQL) == []
        upgrade(url)
        expected = [("va_app", False, True, True, True), ("va_system", True, True, True, True)]
        assert query(url, ROLE_STATE_SQL) == expected
        downgrade(url)
        query(url, "ALTER ROLE va_system NOBYPASSRLS")
        upgrade(url)
        assert query(url, ROLE_STATE_SQL) == expected
    assert [row[:2] for row in query(pg_url, ROLE_STATE_SQL)] == [("va_app", False), ("va_system", True)]
