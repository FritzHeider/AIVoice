"""0007_call_event_type offline and on Postgres (specs/03 §6 ``call_events.event_type``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import ModuleType
from typing import Final

import pytest
from migration_helpers import downgrade, fresh_database, load_revision, offline_sql, query, upgrade

REVISION: Final[ModuleType] = load_revision("0007_call_event_type.py")
INSERT_EVENT: Final = """
INSERT INTO call_events (id, org_id, call_id, seq, category, message, created_at)
VALUES (:id, :org, :call, 1, 'call', 'Call started', :at)
"""
COLUMNS: Final = """
SELECT column_name, is_nullable FROM information_schema.columns
WHERE table_name = 'call_events' ORDER BY ordinal_position
"""


def test_revision_chain() -> None:
    assert (REVISION.revision, REVISION.down_revision) == ("0007", "0006")


def test_offline_upgrade_appends_a_nullable_column() -> None:
    sql = offline_sql("upgrade", "0006:0007")
    assert "ALTER TABLE call_events ADD COLUMN event_type text;" in sql
    assert "UPDATE alembic_version SET version_num='0007' WHERE alembic_version.version_num = '0006'" in sql
    assert "GRANT" not in sql


def test_offline_downgrade_drops_the_column() -> None:
    sql = offline_sql("downgrade", "0007:0006")
    assert "ALTER TABLE call_events DROP COLUMN event_type;" in sql


@pytest.mark.integration
def test_existing_rows_keep_null_and_partitions_get_the_column(pg_url: str) -> None:
    with fresh_database(pg_url) as url:
        upgrade(url, "0006")
        now = datetime.now(UTC)
        event_id = uuid.uuid4()
        query(url, INSERT_EVENT, id=event_id, org=uuid.uuid4(), call=uuid.uuid4(), at=now)
        upgrade(url, "0007")
        assert query(url, "SELECT event_type FROM call_events WHERE id = :id", id=event_id) == [(None,)]
        assert query(url, COLUMNS)[-1] == ("event_type", "YES")
        partition = query(
            url,
            "SELECT tableoid::regclass::text FROM call_events WHERE id = :id",
            id=event_id,
        )[0][0]
        partition_columns = query(
            url,
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t",
            t=partition,
        )
        assert ("event_type",) in partition_columns
        downgrade(url, "0006")
        assert "event_type" not in {row[0] for row in query(url, COLUMNS)}
