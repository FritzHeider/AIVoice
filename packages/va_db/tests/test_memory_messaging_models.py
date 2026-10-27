from __future__ import annotations

from typing import Any

import pytest
from model_introspection import (
    assert_enum_checks_mirror,
    assert_org_indexed,
    check_names,
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
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import BigInteger

from va_db.base import Base
from va_db.models import (
    EMBEDDING_DIMENSIONS,
    Conversation,
    ConversationChannel,
    ConversationRunnerType,
    ConversationStatus,
    MemoryChangeType,
    MessageDirection,
    MessageRole,
    MessageStatus,
    TenDlcBrandStatus,
    TenDlcCampaignStatus,
)

# Column order exactly as in specs/03-data-model.md §7 and §8; a trailing "?" marks a nullable column.
_SPEC_COLUMNS: dict[str, str] = {
    "memories": (
        "id org_id contact_id persona_id? summary facts open_items recent_messages enabled "
        "summary_embedding? created_at updated_at"
    ),
    "memory_changes": (
        "id org_id memory_id call_id? conversation_id? change_type before? after? created_at updated_at"
    ),
    "conversations": (
        "id org_id channel agent_number? user_number? widget_id? widget_visitor_id? contact_id? runner_type "
        "objective? pathway_id? pathway_version? persona_id? agent_id? current_node_id? engine_state "
        "request_data metadata status time_out_minutes last_message_at? webhook? summary? disposition_tag? "
        "created_at updated_at"
    ),
    "messages": (
        "id org_id conversation_id direction role body media_urls telnyx_message_id? status error? segments "
        "price_micros node_id? created_at updated_at"
    ),
    "ten_dlc_brands": ("id org_id telnyx_brand_id? payload status status_detail? created_at updated_at"),
    "ten_dlc_campaigns": (
        "id org_id brand_id telnyx_campaign_id? use_case payload status status_detail? created_at updated_at"
    ),
}

_TABLES = tuple(_SPEC_COLUMNS)


def test_tables_registered_with_spec_columns() -> None:
    assert len(_TABLES) == 6
    assert set(_TABLES) <= set(Base.metadata.tables)
    assert spec_column_mismatches(_SPEC_COLUMNS) == {}


def test_conversation_metadata_maps_to_spec_column() -> None:
    assert Conversation.metadata_.property.columns[0].name == "metadata"
    assert Conversation.metadata is Base.metadata


# (table, column) -> (Literal alias, values in specs/03 §7/§8 order).
_ENUMS: dict[tuple[str, str], tuple[Any, tuple[str, ...]]] = {
    ("memory_changes", "change_type"): (
        MemoryChangeType,
        ("summary", "fact_set", "fact_delete", "open_item_add", "open_item_close", "reset"),
    ),
    ("conversations", "channel"): (ConversationChannel, ("sms", "mms", "widget", "imessage", "rcs")),
    ("conversations", "runner_type"): (ConversationRunnerType, ("objective", "pathway", "persona", "agent")),
    ("conversations", "status"): (ConversationStatus, ("active", "human_takeover", "closed", "timed_out")),
    ("messages", "direction"): (MessageDirection, ("inbound", "outbound")),
    ("messages", "role"): (MessageRole, ("user", "assistant", "human_agent", "system")),
    ("messages", "status"): (
        MessageStatus,
        (
            "queued",
            "sending",
            "sent",
            "delivered",
            "sending_failed",
            "delivery_failed",
            "delivery_unconfirmed",
            "received",
        ),
    ),
    ("ten_dlc_brands", "status"): (
        TenDlcBrandStatus,
        ("draft", "submitted", "verified", "unverified", "failed"),
    ),
    ("ten_dlc_campaigns", "status"): (
        TenDlcCampaignStatus,
        ("draft", "submitted", "active", "rejected", "suspended"),
    ),
}


def test_enum_checks_mirror_literals() -> None:
    assert_enum_checks_mirror(_ENUMS)


def test_check_constraints_are_exactly_the_enum_checks() -> None:
    assert check_names(_TABLES) == enum_check_names(_ENUMS)


def test_server_defaults() -> None:
    assert server_defaults(_TABLES) == {
        ("memories", "summary"): "''",
        ("memories", "facts"): "'{}'::jsonb",
        ("memories", "open_items"): "'[]'::jsonb",
        ("memories", "recent_messages"): "'[]'::jsonb",
        ("memories", "enabled"): "true",
        ("conversations", "engine_state"): "'{}'::jsonb",
        ("conversations", "request_data"): "'{}'::jsonb",
        ("conversations", "metadata"): "'{}'::jsonb",
        ("conversations", "status"): "'active'",
        ("conversations", "time_out_minutes"): "1440",
        ("messages", "media_urls"): "'{}'",
        ("messages", "status"): "'queued'",
        ("messages", "segments"): "1",
        ("messages", "price_micros"): "0",
    }


def test_unique_and_primary_key_names() -> None:
    assert unique_and_pk(_TABLES) == {
        *((f"{table}_pkey", ("id",)) for table in _TABLES),
        ("messages_telnyx_message_id_key", ("telnyx_message_id",)),
        ("ten_dlc_brands_telnyx_brand_id_key", ("telnyx_brand_id",)),
        ("ten_dlc_campaigns_telnyx_campaign_id_key", ("telnyx_campaign_id",)),
    }


# Foreign keys besides each table's ``org_id`` CASCADE: (table, column, target, ondelete).
_OTHER_FKS = {
    ("memories", "contact_id", "contacts.id", "CASCADE"),
    ("memories", "persona_id", "personas.id", "CASCADE"),
    ("memory_changes", "memory_id", "memories.id", "CASCADE"),
    ("conversations", "widget_id", "widgets.id", "SET NULL"),
    ("conversations", "contact_id", "contacts.id", "SET NULL"),
    ("conversations", "pathway_id", "pathways.id", "SET NULL"),
    ("conversations", "persona_id", "personas.id", "SET NULL"),
    ("conversations", "agent_id", "agents.id", "SET NULL"),
    ("messages", "conversation_id", "conversations.id", "CASCADE"),
    ("ten_dlc_campaigns", "brand_id", "ten_dlc_brands.id", "CASCADE"),
}


def test_foreign_keys_ondelete() -> None:
    assert foreign_keys(_TABLES) == expected_foreign_keys(_TABLES, _OTHER_FKS)


@pytest.mark.parametrize("column", ["call_id", "conversation_id"])
def test_memory_change_references_have_no_foreign_keys(column: str) -> None:
    assert not get_table("memory_changes").c[column].foreign_keys


_SPEC_INDEXES = {
    "CREATE UNIQUE INDEX memories_contact_persona_uq ON memories "
    "(org_id, contact_id, coalesce(persona_id, '00000000-0000-0000-0000-000000000000'::uuid))",
    "CREATE INDEX memory_changes_memory_idx ON memory_changes (memory_id, created_at DESC)",
    "CREATE INDEX conversations_lookup_idx ON conversations "
    "(org_id, channel, agent_number, user_number, status)",
    "CREATE INDEX conversations_widget_idx ON conversations (widget_id, widget_visitor_id)",
    "CREATE INDEX messages_conversation_idx ON messages (conversation_id, created_at)",
}
# The specs/03 §1 decision ("§7-§9a likewise") adds these org_id indexes.
_DECISION_ORG_INDEXED = ("memory_changes", "messages", "ten_dlc_brands", "ten_dlc_campaigns")


def test_indexes() -> None:
    assert index_ddl(_TABLES) == _SPEC_INDEXES | org_index_ddl(_DECISION_ORG_INDEXED)


@pytest.mark.parametrize("table", _TABLES)
def test_every_table_has_an_org_id_leading_index(table: str) -> None:
    assert_org_indexed(table)


def test_summary_embedding_is_a_nullable_1024_vector() -> None:
    column = get_table("memories").c.summary_embedding
    assert isinstance(column.type, VECTOR)
    assert column.type.dim == EMBEDDING_DIMENSIONS == 1024
    assert column.nullable is True


def test_column_types() -> None:
    assert type(get_table("messages").c.price_micros.type) is BigInteger
    assert text_array_columns(_TABLES) == {("messages", "media_urls")}


def test_every_timestamp_is_timezone_aware() -> None:
    assert naive_timestamps(_TABLES) == []
