"""0008_pathway_run_publish offline and on Postgres (specs/03 §5 ``pathway_test_runs.publish``)."""

from __future__ import annotations

from types import ModuleType
from typing import Final

import pytest
from migration_helpers import downgrade, fresh_database, load_revision, offline_sql, query, upgrade

REVISION: Final[ModuleType] = load_revision("0008_pathway_run_publish.py")
COLUMNS: Final = """
SELECT column_name, data_type, is_nullable FROM information_schema.columns
WHERE table_name = 'pathway_test_runs' ORDER BY ordinal_position
"""


def test_revision_chain() -> None:
    assert (REVISION.revision, REVISION.down_revision) == ("0008", "0007")


def test_offline_upgrade_appends_a_nullable_column() -> None:
    sql = offline_sql("upgrade", "0007:0008")
    assert "ALTER TABLE pathway_test_runs ADD COLUMN publish jsonb;" in sql
    assert "UPDATE alembic_version SET version_num='0008' WHERE alembic_version.version_num = '0007'" in sql
    assert "GRANT" not in sql


def test_offline_downgrade_drops_the_column() -> None:
    sql = offline_sql("downgrade", "0008:0007")
    assert "ALTER TABLE pathway_test_runs DROP COLUMN publish;" in sql


@pytest.mark.integration
def test_the_column_is_appended_and_dropped(pg_url: str) -> None:
    with fresh_database(pg_url) as url:
        upgrade(url, "0008")
        assert query(url, COLUMNS)[-1] == ("publish", "jsonb", "YES")
        downgrade(url, "0007")
        assert "publish" not in {row[0] for row in query(url, COLUMNS)}
