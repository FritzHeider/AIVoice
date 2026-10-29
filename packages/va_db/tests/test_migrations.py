"""The 0001_initial migration rendered offline, and its statement splitter (specs/03-data-model.md §2-§10)."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from io import StringIO
from types import ModuleType
from typing import Final

import pytest
from alembic import command
from migration_helpers import alembic_config, load_revision
from model_introspection import spec_sql_blocks, spec_table_names

from va_db.base import Base

INITIAL: Final[ModuleType] = load_revision("0001_initial.py")
SPEC_SECTIONS: Final = (
    "TENANCY",
    "TELEPHONY",
    "AGENT_CONFIGURATION",
    "CALLS",
    "MEMORY",
    "MESSAGING",
    "WEBHOOKS_AND_BILLING",
    "ADDITIONAL_TABLES",
)
_INI_LOGGERS: Final = ("alembic", "sqlalchemy.engine")


def _offline(action: str, revision: str, *, configure_logger: bool = False) -> str:
    output = StringIO()
    getattr(command, action)(alembic_config(output, configure_logger=configure_logger), revision, sql=True)
    return output.getvalue()


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _offline("upgrade", "head")


def _without_comments(statement: str) -> str:
    return "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--")).strip()


def test_offline_upgrade_creates_every_model_table(upgrade_sql: str) -> None:
    created = re.findall(r"^CREATE TABLE (\w+) \(", upgrade_sql, re.MULTILINE)
    assert sorted(created) == sorted([*Base.metadata.tables, "alembic_version"])
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001')" in upgrade_sql
    assert (
        "UPDATE alembic_version SET version_num='0002' WHERE alembic_version.version_num = '0001'"
        in upgrade_sql
    )
    assert (
        "UPDATE alembic_version SET version_num='0003' WHERE alembic_version.version_num = '0002'"
        in upgrade_sql
    )
    assert (
        "UPDATE alembic_version SET version_num='0004' WHERE alembic_version.version_num = '0003'"
        in upgrade_sql
    )


def test_offline_upgrade_keeps_spec_ddl(upgrade_sql: str) -> None:
    for fragment in (
        "CREATE EXTENSION IF NOT EXISTS vector;",
        "CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$",
        ") PARTITION BY RANGE (created_at);",
        "CREATE POLICY platform_read ON voices FOR SELECT USING (org_id IS NULL);",
        "CREATE POLICY platform_read ON dnc_entries FOR SELECT USING (org_id IS NULL);",
        "DEFAULT '{}'::jsonb",
        "ON kb_chunks USING hnsw (embedding vector_cosine_ops)",
        "'call_events_p' || to_char(month_start, 'YYYYMM')",
        "GRANT va_app, va_system TO CURRENT_USER;",
        "CREATE ROLE va_system LOGIN BYPASSRLS;",
    ):
        assert fragment in upgrade_sql, fragment


def test_json_literal_colons_are_not_bind_parameters(upgrade_sql: str) -> None:
    assert '\'{"stability":0.5,"similarity_boost":0.75,"style":0,"use_speaker_boost":true,"speed":1.0}\'' in (
        upgrade_sql
    )
    assert "\\:" not in upgrade_sql


def test_grants_cover_exactly_the_revision_tables(upgrade_sql: str) -> None:
    (granted,) = re.findall(
        r"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE (.+) TO va_app, va_system;", upgrade_sql
    )
    assert granted.split(", ") == list(INITIAL.TABLES)
    assert "GRANT USAGE ON SCHEMA public TO va_app, va_system;" in upgrade_sql


def test_offline_downgrade_drops_tables_in_reverse_dependency_order() -> None:
    sql = _offline("downgrade", "0001:base")
    dropped = re.findall(r"^DROP TABLE IF EXISTS (\w+) CASCADE;", sql, re.MULTILINE)
    assert dropped == list(reversed(INITIAL.TABLES))
    assert "DROP FUNCTION IF EXISTS set_updated_at();" in sql
    assert re.findall(r"^DROP EXTENSION IF EXISTS (\w+);", sql, re.MULTILINE) == [
        "citext",
        "pg_trgm",
        "vector",
    ]
    assert "DROP ROLE" not in sql


@pytest.fixture
def restore_ini_loggers() -> Iterator[None]:
    loggers = [logging.getLogger(), *(logging.getLogger(name) for name in _INI_LOGGERS)]
    saved = [(logger, logger.level, list(logger.handlers), logger.propagate) for logger in loggers]
    yield
    for logger, level, handlers, propagate in saved:
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.propagate = propagate


@pytest.mark.usefixtures("restore_ini_loggers")
def test_cli_runs_configure_logging_from_the_ini() -> None:
    _offline("upgrade", "head", configure_logger=True)
    assert logging.getLogger("alembic").level == logging.INFO
    assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING


def test_tables_follow_spec_order_and_match_the_models() -> None:
    assert list(INITIAL.TABLES) == spec_table_names()
    assert set(INITIAL.TABLES) == set(Base.metadata.tables)


def test_spec_blocks_cover_sections_2_to_10() -> None:
    blocks = spec_sql_blocks()
    assert len(blocks) == 11
    assert blocks[0].startswith("CREATE EXTENSION IF NOT EXISTS vector;")
    assert "CREATE POLICY tenant_isolation ON calls" in blocks[-1]
    assert not any("ENGINE =" in block for block in blocks)
    created = [name for block in blocks for name in re.findall(r"CREATE TABLE (\w+)", block)]
    assert created == spec_table_names()


def test_spec_sections_hold_only_create_statements() -> None:
    created = [
        _without_comments(statement)
        for name in SPEC_SECTIONS
        for statement in INITIAL.statements(getattr(INITIAL, name))
    ]
    assert all(statement.startswith("CREATE ") for statement in created)
    assert sum(statement.startswith("CREATE TABLE ") for statement in created) == len(INITIAL.TABLES)


def test_statements_keep_the_function_body_whole() -> None:
    statements = INITIAL.statements(INITIAL.EXTENSIONS_AND_FUNCTIONS)
    assert len(statements) == 4
    assert statements[-1].endswith("RETURN NEW;\nEND;\n$$")


def test_statements_ignore_semicolons_in_literals_and_comments() -> None:
    script = "SELECT 'a;b''c';  -- trailing; comment\n\nSELECT 2;\n-- orgs' only a comment;\n\n"
    assert INITIAL.statements(script) == ["SELECT 'a;b''c'", "-- trailing; comment\n\nSELECT 2"]


def test_statements_of_an_empty_script() -> None:
    assert INITIAL.statements("  \n-- nothing to run\n") == []
