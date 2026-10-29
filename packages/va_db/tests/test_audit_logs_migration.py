"""0004_audit_logs_append_only (specs/28-security.md §6.3): offline SQL and privileges on real Postgres."""

from __future__ import annotations

from types import ModuleType
from typing import Final

import pytest
from migration_helpers import downgrade, fresh_database, load_revision, offline_sql, query, upgrade

REVISION: Final[ModuleType] = load_revision("0004_audit_logs_append_only.py")
_PRIVILEGES_SQL: Final = (
    "SELECT p, has_table_privilege('va_app', 'audit_logs', p), "
    "has_table_privilege('va_system', 'audit_logs', p) "
    "FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p"
)


def test_revision_follows_0003() -> None:
    assert (REVISION.revision, REVISION.down_revision) == ("0004", "0003")


def test_upgrade_revokes_update_and_delete_from_the_app_role_only() -> None:
    sql = offline_sql("upgrade", "0003:0004")
    assert "REVOKE UPDATE, DELETE ON TABLE audit_logs FROM va_app;" in sql
    assert "va_system" not in sql
    assert "UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003'" in sql


def test_downgrade_grants_them_back() -> None:
    sql = offline_sql("downgrade", "0004:0003")
    assert "GRANT UPDATE, DELETE ON TABLE audit_logs TO va_app;" in sql
    assert "UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0004'" in sql


@pytest.mark.integration
def test_privileges_before_and_after_the_revision(pg_url: str) -> None:
    with fresh_database(pg_url) as url:
        upgrade(url)
        assert sorted(query(url, _PRIVILEGES_SQL)) == [
            ("DELETE", False, True),
            ("INSERT", True, True),
            ("SELECT", True, True),
            ("UPDATE", False, True),
        ]
        downgrade(url, "0003")
        assert {row[1] for row in query(url, _PRIVILEGES_SQL)} == {True}
