from __future__ import annotations

from typing import Any

import pytest
from model_introspection import (
    assert_enum_check_mirrors,
    assert_org_indexed,
    check_names,
    constraint,
    expected_foreign_keys,
    foreign_keys,
    get_table,
    index_ddl,
    naive_timestamps,
    org_index_ddl,
    server_defaults,
    spec_column_mismatches,
    unique_and_pk,
)
from sqlalchemy import REAL, BigInteger, CheckConstraint, Numeric, SmallInteger
from sqlalchemy.dialects.postgresql import CITEXT

from va_db.base import Base
from va_db.models import (
    AnsweredBy,
    BatchItemStatus,
    BatchStatus,
    Call,
    CallAnalysisKind,
    CallDirection,
    CallEventCategory,
    CallEventLevel,
    CallLegRole,
    CallLegState,
    CallStatus,
    CallTurn,
    CallTurnRole,
    DncSource,
    EndedBy,
    EvalRunStatus,
    RunnerType,
)

# Column order exactly as in specs/03-data-model.md §6; a trailing "?" marks a nullable column.
_SPEC_COLUMNS: dict[str, str] = {
    "contacts": "id org_id phone_e164? email? external_id? name? attributes created_at updated_at",
    "batches": (
        "id org_id label? status global_props status_webhook? schedule calls_total calls_dispatched "
        "calls_completed calls_successful calls_failed failure_code? failure_reason? started_at? "
        "completed_at? created_by_key? created_at updated_at"
    ),
    "batch_items": (
        "id org_id batch_id ordinal payload status skip_reason? call_id? attempts next_attempt_at? "
        "created_at updated_at"
    ),
    "calls": (
        "id org_id external_id? direction to_number? from_number? contact_id? status queue_status? "
        "answered_by? runner_type pathway_id? pathway_version? persona_id? persona_version? agent_id? "
        "agent_version_id? experiment_id? experiment_arm_id? test_call batch_id? batch_item_id? "
        "parent_call_id? phone_number_id? voice_id? config request_data metadata variables start_time? "
        "dial_started_at? answered_at? "
        "started_at? ended_at? connected_seconds transfer_seconds end_reason? ended_by? transferred_to? "
        "recording_s3_key? recording_duration_ms? summary? disposition_tag? error_message? price_micros "
        "credit_hold_id? workflow_id? memory_id? created_at updated_at"
    ),
    "call_legs": (
        "id org_id call_id role telnyx_account_id? call_control_id call_leg_id? call_session_id? state "
        "hangup_cause? hangup_source? sip_hangup_cause? amd_result? started_at answered_at? ended_at? "
        "created_at updated_at"
    ),
    "call_turns": (
        "id org_id call_id turn_index role text text_spoken? interrupted start_ms end_ms node_id? node_name? "
        "language_code? stt_confidence? latency text_realtime? created_at updated_at"
    ),
    "call_events": "id org_id call_id seq category level message data created_at",
    "call_analyses": ("id org_id call_id kind goal? questions? schema? result model created_at updated_at"),
    "citations": (
        "id org_id call_id citation_schema_id variable_name variable_type value? conditions_met? "
        "cited_utterances created_at updated_at"
    ),
    "eval_runs": (
        "id org_id call_id? eval_setup_version_id status overall_score? passed? results error? "
        "created_at updated_at"
    ),
    "guard_rail_events": (
        "id org_id call_id guard_rail triggered action? turn_index? detail created_at updated_at"
    ),
    "dnc_entries": "id org_id? phone_e164 source reason? call_id? expires_at? created_at updated_at",
}

_TABLES = tuple(_SPEC_COLUMNS)


def test_tables_registered_with_spec_columns() -> None:
    assert len(_TABLES) == 12
    assert set(_TABLES) <= set(Base.metadata.tables)
    assert spec_column_mismatches(_SPEC_COLUMNS) == {}


def test_reserved_and_shadowing_names_map_to_spec_columns() -> None:
    assert Call.metadata_.property.columns[0].name == "metadata"
    assert CallTurn.text_.property.columns[0].name == "text"
    assert Call.metadata is Base.metadata


# (table, column) -> (Literal alias, values in specs/03 §6 order).
_ENUMS: dict[tuple[str, str], tuple[Any, tuple[str, ...]]] = {
    ("batches", "status"): (
        BatchStatus,
        (
            "created",
            "scheduled",
            "validating",
            "dispatching",
            "in_progress",
            "completed",
            "stopped",
            "failed",
        ),
    ),
    ("batch_items", "status"): (
        BatchItemStatus,
        ("pending", "skipped", "dispatched", "completed", "failed", "canceled"),
    ),
    ("calls", "direction"): (CallDirection, ("outbound", "inbound", "web")),
    ("calls", "status"): (
        CallStatus,
        (
            "scheduled",
            "queued",
            "dialing",
            "ringing",
            "in_progress",
            "transferring",
            "transferred",
            "completed",
            "failed",
            "canceled",
            "no_answer",
            "busy",
        ),
    ),
    ("calls", "answered_by"): (AnsweredBy, ("human", "voicemail", "ivr", "unknown", "no_answer", "fax")),
    ("calls", "runner_type"): (RunnerType, ("task", "pathway", "persona", "agent")),
    ("calls", "ended_by"): (
        EndedBy,
        ("agent", "user", "system", "api", "guard_rail", "timeout", "error"),
    ),
    ("call_legs", "role"): (CallLegRole, ("primary", "transfer_target", "warm_proxy", "monitor")),
    ("call_legs", "state"): (CallLegState, ("initiated", "ringing", "answered", "bridged", "hangup")),
    ("call_turns", "role"): (
        CallTurnRole,
        ("user", "assistant", "agent-action", "system", "human_agent"),
    ),
    ("call_events", "category"): (
        CallEventCategory,
        (
            "queue",
            "call",
            "latency",
            "webhook",
            "tool",
            "dynamic_data",
            "route",
            "extraction",
            "guard_rail",
            "transfer",
            "voicemail",
            "error",
            "citations",
            "evals",
            "post_transfer_transcript",
            "sms",
            "memory",
            "interrupt",
        ),
    ),
    ("call_events", "level"): (CallEventLevel, ("debug", "info", "warning", "error")),
    ("call_analyses", "kind"): (
        CallAnalysisKind,
        ("summary", "disposition", "analysis_schema", "sentiment", "qa"),
    ),
    ("eval_runs", "status"): (EvalRunStatus, ("queued", "running", "completed", "failed")),
    ("dnc_entries", "source"): (
        DncSource,
        ("manual", "api", "opt_out_voice", "opt_out_sms", "import", "platform"),
    ),
}


@pytest.mark.parametrize(("key", "mirror"), _ENUMS.items())
def test_enum_checks_mirror_literals(key: tuple[str, str], mirror: tuple[Any, tuple[str, ...]]) -> None:
    assert_enum_check_mirrors(*key, *mirror)


def test_check_constraints_are_exactly_the_spec_set() -> None:
    external_id = constraint("calls", "calls_external_id_check")
    assert isinstance(external_id, CheckConstraint)
    assert str(external_id.sqltext) == "char_length(external_id) <= 256"
    enum_names = {f"{table}_{column}_check" for table, column in _ENUMS}
    assert check_names(_TABLES) == enum_names | {"calls_external_id_check"}


_EMPTY_OBJECT = "'{}'::jsonb"
_EMPTY_LIST = "'[]'::jsonb"


def test_server_defaults() -> None:
    assert server_defaults(_TABLES) == {
        ("contacts", "attributes"): _EMPTY_OBJECT,
        ("batches", "status"): "'created'",
        ("batches", "global_props"): _EMPTY_OBJECT,
        ("batches", "schedule"): _EMPTY_OBJECT,
        ("batches", "calls_total"): "0",
        ("batches", "calls_dispatched"): "0",
        ("batches", "calls_completed"): "0",
        ("batches", "calls_successful"): "0",
        ("batches", "calls_failed"): "0",
        ("batch_items", "status"): "'pending'",
        ("batch_items", "attempts"): "0",
        ("calls", "test_call"): "false",
        ("calls", "request_data"): _EMPTY_OBJECT,
        ("calls", "metadata"): _EMPTY_OBJECT,
        ("calls", "variables"): _EMPTY_OBJECT,
        ("calls", "connected_seconds"): "0",
        ("calls", "transfer_seconds"): "0",
        ("calls", "price_micros"): "0",
        ("call_legs", "started_at"): "now()",
        ("call_turns", "interrupted"): "false",
        ("call_turns", "latency"): _EMPTY_OBJECT,
        ("call_events", "level"): "'info'",
        ("call_events", "data"): _EMPTY_OBJECT,
        ("citations", "cited_utterances"): _EMPTY_LIST,
        ("eval_runs", "results"): _EMPTY_LIST,
        ("guard_rail_events", "detail"): _EMPTY_OBJECT,
    }


def test_call_events_created_at_defaults_to_now_and_id_is_generated_in_python() -> None:
    table = get_table("call_events")
    assert str(table.c.created_at.server_default.arg) == "now()"  # type: ignore[union-attr]
    assert table.c.id.server_default is None
    assert table.c.id.default is not None


def test_unique_and_primary_key_names() -> None:
    assert unique_and_pk(_TABLES) == {
        *((f"{table}_pkey", ("id",)) for table in _TABLES if table != "call_events"),
        ("call_events_pkey", ("created_at", "id")),
        ("batch_items_batch_id_ordinal_key", ("batch_id", "ordinal")),
        ("call_turns_call_id_turn_index_role_key", ("call_id", "turn_index", "role")),
    }


def test_call_events_is_range_partitioned_by_created_at() -> None:
    assert get_table("call_events").dialect_options["postgresql"]["partition_by"] == "RANGE (created_at)"


# Foreign keys besides each table's ``org_id`` CASCADE: (table, column, target, ondelete).
_OTHER_FKS = {
    ("batches", "created_by_key", "api_keys.id", "SET NULL"),
    ("batch_items", "batch_id", "batches.id", "CASCADE"),
    ("calls", "contact_id", "contacts.id", "SET NULL"),
    ("calls", "pathway_id", "pathways.id", "SET NULL"),
    ("calls", "persona_id", "personas.id", "SET NULL"),
    ("calls", "agent_id", "agents.id", "SET NULL"),
    ("calls", "agent_version_id", "agent_versions.id", "SET NULL"),
    ("calls", "experiment_id", "experiments.id", "SET NULL"),
    ("calls", "experiment_arm_id", "experiment_arms.id", "SET NULL"),
    ("calls", "batch_id", "batches.id", "SET NULL"),
    ("calls", "batch_item_id", "batch_items.id", "SET NULL"),
    ("calls", "parent_call_id", "calls.id", "SET NULL"),
    ("calls", "phone_number_id", "phone_numbers.id", "SET NULL"),
    ("calls", "voice_id", "voices.id", "SET NULL"),
    ("call_legs", "call_id", "calls.id", "CASCADE"),
    ("call_legs", "telnyx_account_id", "telnyx_accounts.id", "SET NULL"),
    ("call_turns", "call_id", "calls.id", "CASCADE"),
    ("call_analyses", "call_id", "calls.id", "CASCADE"),
    ("citations", "call_id", "calls.id", "CASCADE"),
    ("citations", "citation_schema_id", "citation_schemas.id", "CASCADE"),
    ("eval_runs", "call_id", "calls.id", "CASCADE"),
    ("eval_runs", "eval_setup_version_id", "eval_setup_versions.id", "CASCADE"),
    ("guard_rail_events", "call_id", "calls.id", "CASCADE"),
}


def test_foreign_keys_ondelete() -> None:
    assert foreign_keys(_TABLES) == expected_foreign_keys(
        (table for table in _TABLES if table != "call_events"), _OTHER_FKS
    )


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("batch_items", "call_id"),
        ("calls", "credit_hold_id"),
        ("calls", "memory_id"),
        ("call_events", "org_id"),
        ("call_events", "call_id"),
        ("dnc_entries", "call_id"),
    ],
)
def test_columns_without_foreign_keys(table: str, column: str) -> None:
    assert not get_table(table).c[column].foreign_keys


_SPEC_INDEXES = {
    "CREATE UNIQUE INDEX contacts_phone_uq ON contacts (org_id, phone_e164) WHERE phone_e164 IS NOT NULL",
    "CREATE UNIQUE INDEX contacts_email_uq ON contacts (org_id, email) WHERE email IS NOT NULL",
    "CREATE UNIQUE INDEX contacts_external_uq ON contacts (org_id, external_id) "
    "WHERE external_id IS NOT NULL",
    "CREATE INDEX batches_org_created_idx ON batches (org_id, created_at DESC)",
    "CREATE INDEX batch_items_dispatch_idx ON batch_items (batch_id, status, next_attempt_at)",
    "CREATE INDEX calls_org_created_idx ON calls (org_id, created_at DESC)",
    "CREATE INDEX calls_org_status_idx ON calls (org_id, status)",
    "CREATE INDEX calls_org_external_idx ON calls (org_id, external_id) WHERE external_id IS NOT NULL",
    "CREATE INDEX calls_org_to_idx ON calls (org_id, to_number, created_at DESC)",
    "CREATE INDEX calls_batch_idx ON calls (batch_id)",
    "CREATE INDEX calls_pathway_idx ON calls (org_id, pathway_id, created_at DESC)",
    "CREATE UNIQUE INDEX call_legs_ccid_uq ON call_legs (call_control_id)",
    "CREATE INDEX call_legs_call_idx ON call_legs (call_id)",
    "CREATE INDEX call_turns_call_idx ON call_turns (call_id, turn_index)",
    "CREATE INDEX call_events_call_idx ON call_events (call_id, seq)",
    "CREATE INDEX call_analyses_call_idx ON call_analyses (call_id)",
    "CREATE INDEX citations_call_idx ON citations (call_id)",
    "CREATE INDEX eval_runs_call_idx ON eval_runs (call_id)",
    "CREATE INDEX guard_rail_events_call_idx ON guard_rail_events (call_id)",
    "CREATE UNIQUE INDEX dnc_scope_phone_uq ON dnc_entries "
    "(coalesce(org_id, '00000000-0000-0000-0000-000000000000'::uuid), phone_e164)",
}
# The specs/03 §1 decision ("§6 likewise") adds these org_id indexes.
_DECISION_ORG_INDEXED = (
    "contacts",
    "batch_items",
    "call_legs",
    "call_turns",
    "call_events",
    "call_analyses",
    "citations",
    "eval_runs",
    "guard_rail_events",
    "dnc_entries",
)


def test_indexes() -> None:
    assert index_ddl(_TABLES) == _SPEC_INDEXES | org_index_ddl(_DECISION_ORG_INDEXED)


@pytest.mark.parametrize("table", _TABLES)
def test_every_table_has_an_org_id_leading_index(table: str) -> None:
    """specs/03 §1: an index that starts with ``org_id``; it is NOT NULL except for the platform DNC list."""
    assert_org_indexed(table, nullable=table == "dnc_entries")


@pytest.mark.parametrize(
    ("table", "column", "sql_type"),
    [
        ("contacts", "email", CITEXT),
        ("batch_items", "attempts", SmallInteger),
        ("calls", "price_micros", BigInteger),
        ("call_turns", "stt_confidence", REAL),
    ],
)
def test_column_types(table: str, column: str, sql_type: type[Any]) -> None:
    assert type(get_table(table).c[column].type) is sql_type


@pytest.mark.parametrize(
    ("table", "column", "precision", "scale"),
    [
        ("calls", "connected_seconds", 10, 3),
        ("calls", "transfer_seconds", 10, 3),
        ("eval_runs", "overall_score", 5, 2),
    ],
)
def test_numeric_columns(table: str, column: str, precision: int, scale: int) -> None:
    numeric = get_table(table).c[column].type
    assert isinstance(numeric, Numeric)
    assert (numeric.precision, numeric.scale, numeric.asdecimal) == (precision, scale, True)


def test_every_timestamp_is_timezone_aware() -> None:
    assert naive_timestamps(_TABLES) == []
