"""0003_session_mfa_passed_at rendered offline (specs/03-data-model.md §3 ``sessions.mfa_passed_at``)."""

from __future__ import annotations

from types import ModuleType
from typing import Final

from migration_helpers import load_revision, offline_sql

REVISION: Final[ModuleType] = load_revision("0003_session_mfa_passed_at.py")


def test_revision_chain() -> None:
    assert (REVISION.revision, REVISION.down_revision) == ("0003", "0002")


def test_offline_upgrade_appends_the_column() -> None:
    sql = offline_sql("upgrade", "0002:0003")
    assert "ALTER TABLE sessions ADD COLUMN mfa_passed_at timestamptz;" in sql
    assert "UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002'" in sql
    assert "CREATE TABLE" not in sql
    assert "GRANT" not in sql


def test_offline_downgrade_drops_the_column() -> None:
    sql = offline_sql("downgrade", "0003:0002")
    assert "ALTER TABLE sessions DROP COLUMN mfa_passed_at;" in sql
    assert "UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0003'" in sql
