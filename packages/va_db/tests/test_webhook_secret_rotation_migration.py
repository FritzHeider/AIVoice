"""0006_webhook_secret_rotation offline and on Postgres (specs/03 §3 ``orgs.webhook_signing_secret_*``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import ModuleType
from typing import Final

import pytest
from migration_helpers import downgrade, fresh_database, load_revision, offline_sql, query, upgrade

REVISION: Final[ModuleType] = load_revision("0006_webhook_secret_rotation.py")
CREATED: Final = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)
INSERT_ORG: Final = """
INSERT INTO orgs (id, name, slug, webhook_signing_secret_enc, created_at, updated_at)
VALUES (:id, 'Org', :slug, '\\x00'::bytea, :at, :at)
"""
ROTATION_COLUMNS: Final = """
SELECT webhook_signing_secret_created_at, webhook_signing_secret_prev_enc,
       webhook_signing_secret_prev_expires_at
FROM orgs WHERE id = :id
"""
COLUMN_NAMES: Final = """
SELECT column_name FROM information_schema.columns
WHERE table_name = 'orgs' AND column_name LIKE 'webhook_signing_secret_%' ORDER BY ordinal_position
"""


def test_revision_chain() -> None:
    assert (REVISION.revision, REVISION.down_revision) == ("0006", "0005")


def test_offline_upgrade_appends_the_columns_and_backfills() -> None:
    sql = offline_sql("upgrade", "0005:0006")
    for statement in (
        "ALTER TABLE orgs ADD COLUMN webhook_signing_secret_created_at timestamptz NOT NULL DEFAULT now();",
        "ALTER TABLE orgs ADD COLUMN webhook_signing_secret_prev_enc bytea;",
        "ALTER TABLE orgs ADD COLUMN webhook_signing_secret_prev_expires_at timestamptz;",
        "UPDATE orgs SET webhook_signing_secret_created_at = created_at;",
        "UPDATE alembic_version SET version_num='0006' WHERE alembic_version.version_num = '0005'",
    ):
        assert statement in sql
    assert "CREATE TABLE" not in sql
    assert "GRANT" not in sql


def test_offline_downgrade_drops_the_columns() -> None:
    sql = offline_sql("downgrade", "0006:0005")
    for column in (
        "webhook_signing_secret_prev_expires_at",
        "webhook_signing_secret_prev_enc",
        "webhook_signing_secret_created_at",
    ):
        assert f"ALTER TABLE orgs DROP COLUMN {column};" in sql
    assert "UPDATE alembic_version SET version_num='0005' WHERE alembic_version.version_num = '0006'" in sql


@pytest.mark.integration
def test_existing_orgs_get_their_creation_time_as_secret_time(pg_url: str) -> None:
    with fresh_database(pg_url) as url:
        upgrade(url, "0005")
        org_id = uuid.uuid4()
        query(url, INSERT_ORG, id=org_id, slug=f"org-{org_id.hex}", at=CREATED)
        upgrade(url, "0006")
        assert query(url, ROTATION_COLUMNS, id=org_id) == [(CREATED, None, None)]
        assert [row[0] for row in query(url, COLUMN_NAMES)] == [
            "webhook_signing_secret_enc",
            "webhook_signing_secret_created_at",
            "webhook_signing_secret_prev_enc",
            "webhook_signing_secret_prev_expires_at",
        ]
        downgrade(url, "0005")
        assert [row[0] for row in query(url, COLUMN_NAMES)] == ["webhook_signing_secret_enc"]
