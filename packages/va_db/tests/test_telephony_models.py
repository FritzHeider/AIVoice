from __future__ import annotations

from typing import Any

import pytest
from model_introspection import (
    assert_enum_check_mirrors,
    check_names,
    columns,
    foreign_keys,
    get_table,
    index_ddl,
    naive_timestamps,
    server_defaults,
    text_array_columns,
    unique_and_pk,
)
from sqlalchemy import BigInteger, Date, DateTime, Integer, LargeBinary, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from va_db.base import Base
from va_db.models import (
    NumberOrderStatus,
    NumberPoolStrategy,
    NumberType,
    PhoneNumberStatus,
    TelnyxAccountStatus,
)

_TABLES = (
    "telnyx_accounts",
    "phone_numbers",
    "number_orders",
    "number_pools",
    "number_pool_members",
    "telnyx_events",
)

# Column order and nullability exactly as in specs/03-data-model.md §4 (True = nullable).
_COLUMNS: dict[str, list[tuple[str, bool]]] = {
    "telnyx_accounts": [
        ("id", False),
        ("org_id", False),
        ("label", False),
        ("api_key_enc", False),
        ("public_key", False),
        ("call_control_app_id", True),
        ("messaging_profile_id", True),
        ("outbound_voice_profile_id", True),
        ("encrypted_key_handle", False),
        ("status", False),
        ("status_detail", True),
        ("created_at", False),
        ("updated_at", False),
    ],
    "phone_numbers": [
        ("id", False),
        ("org_id", False),
        ("e164", False),
        ("telnyx_account_id", True),
        ("telnyx_phone_number_id", True),
        ("country_code", False),
        ("number_type", False),
        ("label", True),
        ("capabilities", False),
        ("inbound_config", False),
        ("sms_config", False),
        ("monthly_cost_micros", False),
        ("status", False),
        ("purchased_at", True),
        ("released_at", True),
        ("deleted_at", True),
        ("created_at", False),
        ("updated_at", False),
    ],
    "number_orders": [
        ("id", False),
        ("org_id", False),
        ("telnyx_order_id", True),
        ("requested_numbers", False),
        ("status", False),
        ("error", True),
        ("created_at", False),
        ("updated_at", False),
    ],
    "number_pools": [
        ("id", False),
        ("org_id", False),
        ("name", False),
        ("strategy", False),
        ("max_calls_per_number_per_day", False),
        ("created_at", False),
        ("updated_at", False),
    ],
    "number_pool_members": [
        ("pool_id", False),
        ("phone_number_id", False),
        ("org_id", False),
        ("last_used_at", True),
        ("calls_today", False),
        ("calls_today_date", True),
        ("created_at", False),
        ("updated_at", False),
    ],
    "telnyx_events": [
        ("id", False),
        ("telnyx_event_id", False),
        ("event_type", False),
        ("org_id", True),
        ("call_id", True),
        ("call_control_id", True),
        ("occurred_at", False),
        ("payload", False),
        ("processed_at", True),
        ("process_error", True),
        ("created_at", False),
        ("updated_at", False),
    ],
}


def test_tables_registered_with_spec_columns() -> None:
    assert {table: columns(table) for table in _TABLES} == _COLUMNS
    assert set(_COLUMNS) <= set(Base.metadata.tables)


# (table, column) -> (Literal alias, values in specs/03 §4 order).
_ENUMS: dict[tuple[str, str], tuple[Any, tuple[str, ...]]] = {
    ("telnyx_accounts", "status"): (TelnyxAccountStatus, ("pending", "active", "error", "deleted")),
    ("phone_numbers", "number_type"): (NumberType, ("local", "toll_free", "mobile", "national")),
    ("phone_numbers", "status"): (PhoneNumberStatus, ("pending", "active", "releasing", "released")),
    ("number_orders", "status"): (NumberOrderStatus, ("pending", "success", "failure")),
    ("number_pools", "strategy"): (
        NumberPoolStrategy,
        ("round_robin", "least_recently_used", "random", "local_match"),
    ),
}


@pytest.mark.parametrize("key", _ENUMS)
def test_enum_checks_mirror_literals(key: tuple[str, str]) -> None:
    assert_enum_check_mirrors(*key, *_ENUMS[key])


def test_check_constraints_are_exactly_the_spec_set() -> None:
    assert check_names(_TABLES) == {
        "telnyx_accounts_status_check",
        "phone_numbers_number_type_check",
        "phone_numbers_status_check",
        "number_orders_status_check",
        "number_pools_strategy_check",
    }


def test_server_defaults() -> None:
    assert server_defaults(_TABLES) == {
        ("telnyx_accounts", "status"): "'pending'",
        ("phone_numbers", "number_type"): "'local'",
        ("phone_numbers", "capabilities"): "'{voice}'",
        ("phone_numbers", "inbound_config"): "'{}'::jsonb",
        ("phone_numbers", "sms_config"): "'{}'::jsonb",
        ("phone_numbers", "monthly_cost_micros"): "0",
        ("phone_numbers", "status"): "'active'",
        ("number_pools", "strategy"): "'round_robin'",
        ("number_pools", "max_calls_per_number_per_day"): "100",
        ("number_pool_members", "calls_today"): "0",
    }


def test_unique_and_primary_key_names() -> None:
    assert unique_and_pk(_TABLES) == {
        *((f"{table}_pkey", ("id",)) for table in _TABLES if table != "number_pool_members"),
        ("number_pool_members_pkey", ("pool_id", "phone_number_id")),
        ("telnyx_accounts_encrypted_key_handle_key", ("encrypted_key_handle",)),
        ("number_pools_org_id_name_key", ("org_id", "name")),
        ("telnyx_events_telnyx_event_id_key", ("telnyx_event_id",)),
    }


def test_foreign_keys_ondelete() -> None:
    assert foreign_keys(_TABLES) == {
        ("telnyx_accounts_org_id_fkey", "telnyx_accounts", "org_id", "orgs.id", "CASCADE"),
        ("phone_numbers_org_id_fkey", "phone_numbers", "org_id", "orgs.id", "CASCADE"),
        (
            "phone_numbers_telnyx_account_id_fkey",
            "phone_numbers",
            "telnyx_account_id",
            "telnyx_accounts.id",
            "SET NULL",
        ),
        ("number_orders_org_id_fkey", "number_orders", "org_id", "orgs.id", "CASCADE"),
        ("number_pools_org_id_fkey", "number_pools", "org_id", "orgs.id", "CASCADE"),
        ("number_pool_members_pool_id_fkey", "number_pool_members", "pool_id", "number_pools.id", "CASCADE"),
        (
            "number_pool_members_phone_number_id_fkey",
            "number_pool_members",
            "phone_number_id",
            "phone_numbers.id",
            "CASCADE",
        ),
        ("number_pool_members_org_id_fkey", "number_pool_members", "org_id", "orgs.id", "CASCADE"),
        ("telnyx_events_org_id_fkey", "telnyx_events", "org_id", "orgs.id", "CASCADE"),
    }


def test_telnyx_event_call_id_has_no_foreign_key() -> None:
    assert not get_table("telnyx_events").c.call_id.foreign_keys


def test_indexes() -> None:
    assert index_ddl(_TABLES) == {
        "CREATE INDEX telnyx_accounts_org_idx ON telnyx_accounts (org_id)",
        "CREATE UNIQUE INDEX phone_numbers_e164_active_uq ON phone_numbers (e164) "
        "WHERE status IN ('pending', 'active')",
        "CREATE INDEX phone_numbers_org_idx ON phone_numbers (org_id)",
        "CREATE INDEX number_orders_org_idx ON number_orders (org_id)",
        "CREATE INDEX number_pool_members_org_idx ON number_pool_members (org_id)",
        "CREATE INDEX telnyx_events_call_idx ON telnyx_events (call_id)",
        "CREATE INDEX telnyx_events_ccid_idx ON telnyx_events (call_control_id)",
    }


@pytest.mark.parametrize(
    ("table", "column", "sql_type"),
    [
        ("telnyx_accounts", "api_key_enc", LargeBinary),
        ("telnyx_accounts", "encrypted_key_handle", Text),
        ("phone_numbers", "inbound_config", JSONB),
        ("phone_numbers", "sms_config", JSONB),
        ("telnyx_events", "payload", JSONB),
        ("phone_numbers", "monthly_cost_micros", BigInteger),
        ("number_pools", "max_calls_per_number_per_day", Integer),
        ("number_pool_members", "calls_today", Integer),
        ("number_pool_members", "calls_today_date", Date),
        ("telnyx_events", "call_id", Uuid),
        ("phone_numbers", "telnyx_account_id", Uuid),
        ("phone_numbers", "status", Text),
        ("telnyx_events", "occurred_at", DateTime),
    ],
)
def test_column_types(table: str, column: str, sql_type: type[Any]) -> None:
    assert type(get_table(table).c[column].type) is sql_type


def test_text_array_columns() -> None:
    assert text_array_columns(_TABLES) == {
        ("phone_numbers", "capabilities"),
        ("number_orders", "requested_numbers"),
    }


def test_every_timestamp_is_timezone_aware() -> None:
    assert naive_timestamps(_TABLES) == []
