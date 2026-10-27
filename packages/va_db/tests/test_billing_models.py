from __future__ import annotations

from typing import Any

import pytest
from model_introspection import (
    assert_enum_checks_mirror,
    assert_org_indexed,
    check_names,
    constraint,
    enum_check_names,
    expected_foreign_keys,
    foreign_keys,
    get_table,
    index_ddl,
    naive_timestamps,
    org_index_ddl,
    server_defaults,
    spec_column_mismatches,
    text_array_columns,
    unique_and_pk,
)
from sqlalchemy import BigInteger, CheckConstraint, LargeBinary, Numeric, SmallInteger, Text

from va_db.base import Base
from va_db.models import (
    CreditEntryType,
    CreditHoldStatus,
    InvoiceStatus,
    JobStatus,
    OrgDataKeyStatus,
    SubscriptionStatus,
    UsageMeter,
    WebhookDeliveryStatus,
    WebhookFormat,
)

# Column order exactly as in specs/03-data-model.md §9 and §9a; a trailing "?" marks a nullable column.
_SPEC_COLUMNS: dict[str, str] = {
    "webhook_endpoints": ("id org_id url description event_types enabled phi_allowed created_at updated_at"),
    "webhook_deliveries": (
        "id org_id event_id event_type url endpoint_id? payload format status attempts next_attempt_at "
        "last_status_code? last_error? last_response_ms? call_id? batch_id? conversation_id? delivered_at? "
        "created_at updated_at"
    ),
    "plans": (
        "id name monthly_fee_micros connected_rate_micros_per_min transfer_rate_micros_per_min "
        "min_attempt_micros sms_rate_micros widget_message_micros tts_rate_micros_per_1k_chars "
        "daily_call_cap? hourly_call_cap? concurrency_cap? voice_clone_cap? features stripe_price_id? "
        "is_public created_at updated_at"
    ),
    "org_subscriptions": (
        "id org_id plan_id status stripe_subscription_id? current_period_start current_period_end "
        "cancel_at_period_end created_at updated_at"
    ),
    "credit_ledger": (
        "id org_id entry_type amount_micros balance_after_micros reference_type? reference_id? description "
        "idempotency_key created_at updated_at"
    ),
    "credit_holds": (
        "id org_id reference_type reference_id amount_micros status expires_at created_at updated_at"
    ),
    "usage_events": (
        "id org_id meter quantity unit_price_micros amount_micros cost_micros reference_type reference_id "
        "idempotency_key occurred_at created_at updated_at"
    ),
    "invoices": (
        "id org_id period_start period_end subtotal_micros credits_applied_micros total_micros line_items "
        "status stripe_invoice_id? pdf_s3_key? created_at updated_at"
    ),
    "stripe_events": "id type payload processed_at? created_at updated_at",
    "org_llm_providers": (
        "id org_id base_url api_key_enc models supports_tool_choice baa_covered last_probe_at? "
        "last_probe_error? created_at updated_at"
    ),
    "dnc_imports": (
        "id org_id status s3_key total? inserted? invalid? duplicates? error? created_at updated_at"
    ),
    "erasure_requests": "id org_id selector status result? created_at updated_at",
    "org_secrets": "id org_id name value_enc created_at updated_at",
    "org_data_keys": "id org_id kek_id encrypted_data_key status created_at updated_at",
}

_TABLES = tuple(_SPEC_COLUMNS)
_SYSTEM_TABLES = ("plans", "stripe_events")
_TENANT_TABLES = tuple(table for table in _TABLES if table not in _SYSTEM_TABLES)


def test_tables_registered_with_spec_columns() -> None:
    assert len(_TABLES) == 14
    assert set(_TABLES) <= set(Base.metadata.tables)
    assert spec_column_mismatches(_SPEC_COLUMNS) == {}


_JOB_STATUSES = ("queued", "running", "completed", "failed")
# (table, column) -> (Literal alias, values in specs/03 §9/§9a order).
_ENUMS: dict[tuple[str, str], tuple[Any, tuple[str, ...]]] = {
    ("webhook_deliveries", "format"): (WebhookFormat, ("bland", "envelope")),
    ("webhook_deliveries", "status"): (
        WebhookDeliveryStatus,
        ("pending", "delivering", "succeeded", "failed", "dead"),
    ),
    ("org_subscriptions", "status"): (SubscriptionStatus, ("active", "past_due", "canceled", "trialing")),
    ("credit_ledger", "entry_type"): (
        CreditEntryType,
        (
            "purchase",
            "auto_recharge",
            "grant",
            "refund",
            "usage",
            "adjustment",
            "subscription_fee",
            "hold",
            "hold_release",
            "transfer_in",
            "transfer_out",
        ),
    ),
    ("credit_holds", "status"): (CreditHoldStatus, ("held", "captured", "released")),
    ("usage_events", "meter"): (
        UsageMeter,
        (
            "call_connected_seconds",
            "call_transfer_seconds",
            "call_attempt",
            "sms_segment_out",
            "sms_segment_in",
            "widget_message",
            "tts_characters",
            "kb_embedding_tokens",
            "voice_clone",
            "phone_number_month",
            "llm_tokens_in",
            "llm_tokens_out",
            "stt_seconds",
        ),
    ),
    ("invoices", "status"): (InvoiceStatus, ("draft", "finalized", "paid", "void")),
    ("dnc_imports", "status"): (JobStatus, _JOB_STATUSES),
    ("erasure_requests", "status"): (JobStatus, _JOB_STATUSES),
    ("org_data_keys", "status"): (OrgDataKeyStatus, ("active", "retired")),
}


def test_enum_checks_mirror_literals() -> None:
    assert_enum_checks_mirror(_ENUMS)


def test_check_constraints_are_exactly_the_spec_set() -> None:
    name_check = constraint("org_secrets", "org_secrets_name_check")
    assert isinstance(name_check, CheckConstraint)
    assert str(name_check.sqltext) == "name ~ '^[A-Z][A-Z0-9_]{0,63}$'"
    assert check_names(_TABLES) == enum_check_names(_ENUMS) | {"org_secrets_name_check"}


def test_server_defaults() -> None:
    assert server_defaults(_TABLES) == {
        ("webhook_endpoints", "description"): "''",
        ("webhook_endpoints", "event_types"): "'{*}'",
        ("webhook_endpoints", "enabled"): "true",
        ("webhook_endpoints", "phi_allowed"): "false",
        ("webhook_deliveries", "format"): "'bland'",
        ("webhook_deliveries", "status"): "'pending'",
        ("webhook_deliveries", "attempts"): "0",
        ("webhook_deliveries", "next_attempt_at"): "now()",
        ("plans", "tts_rate_micros_per_1k_chars"): "0",
        ("plans", "features"): "'{}'",
        ("plans", "is_public"): "true",
        ("org_subscriptions", "cancel_at_period_end"): "false",
        ("credit_ledger", "description"): "''",
        ("usage_events", "cost_micros"): "0",
        ("org_llm_providers", "supports_tool_choice"): "true",
        ("org_llm_providers", "baa_covered"): "false",
    }


def test_unique_and_primary_key_names() -> None:
    assert unique_and_pk(_TABLES) == {
        *((f"{table}_pkey", ("id",)) for table in _TABLES),
        ("webhook_deliveries_event_id_url_key", ("event_id", "url")),
        ("org_subscriptions_stripe_subscription_id_key", ("stripe_subscription_id",)),
        ("credit_ledger_org_id_idempotency_key_key", ("org_id", "idempotency_key")),
        ("usage_events_org_id_idempotency_key_key", ("org_id", "idempotency_key")),
        ("invoices_stripe_invoice_id_key", ("stripe_invoice_id",)),
        ("org_llm_providers_org_id_key", ("org_id",)),
        ("org_secrets_org_id_name_key", ("org_id", "name")),
    }


@pytest.mark.parametrize("table", _SYSTEM_TABLES)
def test_system_tables_have_text_ids_and_no_org_id(table: str) -> None:
    columns = get_table(table).c
    assert "org_id" not in columns
    assert type(columns.id.type) is Text
    assert columns.id.primary_key
    assert columns.id.default is None


def test_foreign_keys_ondelete() -> None:
    other_fks = {
        ("webhook_deliveries", "endpoint_id", "webhook_endpoints.id", "SET NULL"),
        # No ON DELETE clause in the spec: NO ACTION, so a subscribed plan cannot be deleted.
        ("org_subscriptions", "plan_id", "plans.id", None),
    }
    assert foreign_keys(_TABLES) == expected_foreign_keys(_TENANT_TABLES, other_fks)


@pytest.mark.parametrize("column", ["call_id", "batch_id", "conversation_id"])
def test_webhook_delivery_references_have_no_foreign_keys(column: str) -> None:
    assert not get_table("webhook_deliveries").c[column].foreign_keys


_SPEC_INDEXES = {
    "CREATE INDEX webhook_endpoints_org_idx ON webhook_endpoints (org_id)",
    "CREATE INDEX webhook_deliveries_due_idx ON webhook_deliveries (next_attempt_at) "
    "WHERE status IN ('pending', 'delivering')",
    "CREATE INDEX webhook_deliveries_org_idx ON webhook_deliveries (org_id, created_at DESC)",
    "CREATE UNIQUE INDEX org_subscriptions_active_uq ON org_subscriptions (org_id) "
    "WHERE status IN ('active', 'past_due', 'trialing')",
    "CREATE INDEX credit_ledger_org_created_idx ON credit_ledger (org_id, created_at DESC)",
    "CREATE INDEX credit_holds_org_status_idx ON credit_holds (org_id, status)",
    "CREATE INDEX usage_events_org_time_idx ON usage_events (org_id, occurred_at DESC)",
    "CREATE INDEX invoices_org_idx ON invoices (org_id, period_start DESC)",
    "CREATE INDEX dnc_imports_org_idx ON dnc_imports (org_id, created_at DESC)",
    "CREATE INDEX erasure_requests_org_idx ON erasure_requests (org_id, created_at DESC)",
    "CREATE UNIQUE INDEX org_data_keys_active_uq ON org_data_keys (org_id) WHERE status = 'active'",
}
# The specs/03 §1 decision ("§7-§9a likewise") adds these org_id indexes.
_DECISION_ORG_INDEXED = ("org_subscriptions", "org_data_keys")


def test_indexes() -> None:
    assert index_ddl(_TABLES) == _SPEC_INDEXES | org_index_ddl(_DECISION_ORG_INDEXED)


@pytest.mark.parametrize("table", _TENANT_TABLES)
def test_every_tenant_table_has_an_org_id_leading_index(table: str) -> None:
    assert_org_indexed(table)


@pytest.mark.parametrize(
    ("table", "column", "sql_type"),
    [
        ("webhook_deliveries", "attempts", SmallInteger),
        ("plans", "monthly_fee_micros", BigInteger),
        ("plans", "tts_rate_micros_per_1k_chars", BigInteger),
        ("credit_ledger", "amount_micros", BigInteger),
        ("credit_holds", "amount_micros", BigInteger),
        ("usage_events", "cost_micros", BigInteger),
        ("invoices", "total_micros", BigInteger),
        ("org_llm_providers", "api_key_enc", LargeBinary),
        ("org_secrets", "value_enc", LargeBinary),
        ("org_data_keys", "encrypted_data_key", LargeBinary),
    ],
)
def test_column_types(table: str, column: str, sql_type: type[Any]) -> None:
    assert type(get_table(table).c[column].type) is sql_type


def test_usage_quantity_is_numeric_18_6() -> None:
    quantity = get_table("usage_events").c.quantity.type
    assert isinstance(quantity, Numeric)
    assert (quantity.precision, quantity.scale, quantity.asdecimal) == (18, 6, True)


def test_text_array_columns() -> None:
    assert text_array_columns(_TABLES) == {("webhook_endpoints", "event_types"), ("plans", "features")}


def test_every_timestamp_is_timezone_aware() -> None:
    assert naive_timestamps(_TABLES) == []
