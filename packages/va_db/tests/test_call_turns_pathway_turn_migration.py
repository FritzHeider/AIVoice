"""0009_call_turns_pathway_turn offline and on Postgres (specs/03 §6 ``call_turns.pathway_turn``)."""

from __future__ import annotations

from types import ModuleType
from typing import Final

import pytest
from migration_helpers import downgrade, fresh_database, load_revision, offline_sql, query, upgrade

REVISION: Final[ModuleType] = load_revision("0009_call_turns_pathway_turn.py")
COLUMNS: Final = """
SELECT column_name, data_type, is_nullable FROM information_schema.columns
WHERE table_name = 'call_turns' ORDER BY ordinal_position
"""


def test_revision_chain() -> None:
    assert (REVISION.revision, REVISION.down_revision) == ("0009", "0008")


def test_offline_upgrade_appends_a_nullable_column() -> None:
    sql = offline_sql("upgrade", "0008:0009")
    assert "ALTER TABLE call_turns ADD COLUMN pathway_turn integer;" in sql
    assert "UPDATE alembic_version SET version_num='0009' WHERE alembic_version.version_num = '0008'" in sql
    assert "GRANT" not in sql


def test_offline_downgrade_drops_the_column() -> None:
    sql = offline_sql("downgrade", "0009:0008")
    assert "ALTER TABLE call_turns DROP COLUMN pathway_turn;" in sql


@pytest.mark.integration
def test_the_column_is_appended_and_dropped(pg_url: str) -> None:
    with fresh_database(pg_url) as url:
        upgrade(url, "0009")
        assert query(url, COLUMNS)[-1] == ("pathway_turn", "integer", "YES")
        downgrade(url, "0008")
        assert "pathway_turn" not in {row[0] for row in query(url, COLUMNS)}
