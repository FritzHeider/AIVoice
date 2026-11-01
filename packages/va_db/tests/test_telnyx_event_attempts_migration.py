"""0005_telnyx_event_attempts rendered offline (specs/03 §4 ``telnyx_events.process_attempts``)."""

from __future__ import annotations

from types import ModuleType
from typing import Final

from migration_helpers import load_revision, offline_sql

REVISION: Final[ModuleType] = load_revision("0005_telnyx_event_attempts.py")


def test_revision_chain() -> None:
    assert (REVISION.revision, REVISION.down_revision) == ("0005", "0004")


def test_offline_upgrade_appends_the_column() -> None:
    sql = offline_sql("upgrade", "0004:0005")
    assert "ALTER TABLE telnyx_events ADD COLUMN process_attempts integer NOT NULL DEFAULT 0;" in sql
    assert "UPDATE alembic_version SET version_num='0005' WHERE alembic_version.version_num = '0004'" in sql
    assert "CREATE TABLE" not in sql
    assert "GRANT" not in sql


def test_offline_downgrade_drops_the_column() -> None:
    sql = offline_sql("downgrade", "0005:0004")
    assert "ALTER TABLE telnyx_events DROP COLUMN process_attempts;" in sql
    assert "UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0005'" in sql
