"""The RLS harness's table selection and generic values (no database)."""

from __future__ import annotations

import re
from typing import get_args

import pytest
from sqlalchemy import Column, Interval, MetaData, Table
from tenant_rows import PLATFORM_READ_TABLES, TENANT_TABLE_NAMES, VALUE_OVERRIDES, column_value

from va_db.base import Base
from va_db.models.calls import CallDirection


def test_tenant_table_names_follow_section_10_rule() -> None:
    assert len(TENANT_TABLE_NAMES) == 65
    assert list(TENANT_TABLE_NAMES) == sorted(TENANT_TABLE_NAMES)


def test_tenant_tables_include_platform_read_and_exclude_system_scope() -> None:
    assert set(PLATFORM_READ_TABLES) <= set(TENANT_TABLE_NAMES)
    assert {"calls", "call_events", "idempotency_keys", "number_pool_members"} <= set(TENANT_TABLE_NAMES)
    assert not {"plans", "telnyx_events", "orgs", "users", "stripe_events"} & set(TENANT_TABLE_NAMES)


def test_overrides_name_real_columns() -> None:
    for table_name, column_name in VALUE_OVERRIDES:
        assert column_name in Base.metadata.tables[table_name].c


def test_enum_columns_take_the_first_allowed_value() -> None:
    calls = Base.metadata.tables["calls"]
    assert column_value(calls, calls.c.direction) == get_args(CallDirection)[0]


def test_regex_checked_columns_use_their_override() -> None:
    dispositions = Base.metadata.tables["dispositions"]
    assert re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", column_value(dispositions, dispositions.c.tag))


def test_unknown_column_type_fails_loudly() -> None:
    table = Table("probe", MetaData(), Column("span", Interval, nullable=False))
    with pytest.raises(TypeError, match=r"probe\.span"):
        column_value(table, table.c.span)
