from __future__ import annotations

from typing import Any

import pytest
from model_introspection import (
    assert_enum_check_mirrors,
    check_names,
    computed_columns,
    constraint,
    foreign_keys,
    get_table,
    index_ddl,
    leading_columns,
    naive_timestamps,
    server_defaults,
    spec_column_mismatches,
    text_array_columns,
    unique_and_pk,
)
from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
)
from sqlalchemy.dialects.postgresql import TSVECTOR

from va_db.base import Base
from va_db.models import (
    EMBEDDING_DIMENSIONS,
    ExperimentStatus,
    KbDocumentType,
    KnowledgeBaseStatus,
    PathwayTestRunKind,
    PathwayTestRunStatus,
    ToolHttpMethod,
    VersionStatus,
    VoiceCategory,
    VoiceCloneStatus,
    VoiceProvider,
    VoiceService,
)

# Column order exactly as in specs/03-data-model.md §5; a trailing "?" marks a nullable column.
_SPEC_COLUMNS: dict[str, str] = {
    "prompts": "id org_id name content tags deleted_at? created_at updated_at",
    "voices": (
        "id org_id? name description provider provider_voice_id service category is_public tags "
        "language_codes gender? accent? age? preview_url? settings rating_sum rating_count clone_status? "
        "consent? clone_error? deleted_at? created_at updated_at"
    ),
    "voice_samples": (
        "id org_id voice_id s3_key mime_type duration_ms transcript? provider_sample_id? "
        "created_at updated_at"
    ),
    "voice_ratings": "voice_id org_id user_id rating created_at updated_at",
    "tools": (
        "id org_id name description speech? url method headers secret_headers_enc? query body? input_schema "
        "response_map timeout_ms retries deleted_at? created_at updated_at"
    ),
    "knowledge_bases": (
        "id org_id name description status embedding_model embedding_dimensions chunk_size_tokens "
        "chunk_overlap_tokens document_count chunk_count deleted_at? created_at updated_at"
    ),
    "kb_documents": (
        "id org_id kb_id type name source_url? s3_key? mime_type? size_bytes? content_sha256? status error? "
        "token_count? created_at updated_at"
    ),
    "kb_chunks": (
        "id org_id kb_id document_id ordinal content token_count heading_path source_url? page_number? "
        "embedding tsv? created_at updated_at"
    ),
    "pathway_folders": "id org_id name parent_id? created_at updated_at",
    "pathways": (
        "id org_id folder_id? name description production_version? staging_version? draft_version "
        "latest_version deleted_at? created_at updated_at"
    ),
    "pathway_versions": (
        "id org_id pathway_id version_number name? status nodes edges global_config graph_hash published_at? "
        "published_by? created_at updated_at"
    ),
    "pathway_unit_tests": (
        "id org_id pathway_id name node_id source_call_id? history user_message expected "
        "simulated_variations created_at updated_at"
    ),
    "pathway_test_runs": (
        "id org_id pathway_id version_number kind status results started_at? finished_at? "
        "created_at updated_at"
    ),
    "personas": (
        "id org_id name description production_version? draft_version latest_version deleted_at? "
        "created_at updated_at"
    ),
    "persona_versions": (
        "id org_id persona_id version_number status config published_at? created_at updated_at"
    ),
    "agents": "id org_id name description env_pins deleted_at? created_at updated_at",
    "agent_versions": (
        "id org_id agent_id semver branch pathway_id? pathway_version? call_settings changelog created_by? "
        "created_at updated_at"
    ),
    "experiments": (
        "id org_id agent_id name status primary_metric primary_metric_config started_at? stopped_at? "
        "created_at updated_at"
    ),
    "experiment_arms": "id org_id experiment_id agent_version_id weight label created_at updated_at",
    "dispositions": "id org_id tag description created_at updated_at",
    "citation_schemas": "id org_id name description variables created_at updated_at",
    "eval_setups": "id org_id name description latest_version created_at updated_at",
    "eval_setup_versions": (
        "id org_id eval_setup_id version_number criteria pass_threshold created_at updated_at"
    ),
    "widgets": (
        "id org_id name pathway_id? pathway_version? agent_id? persona_id? agent_prompt? allowed_domains "
        "messages_per_minute voice_enabled voice_id? config webhook_url? public_key deleted_at? "
        "created_at updated_at"
    ),
}

_TABLES = tuple(_SPEC_COLUMNS)


def test_tables_registered_with_spec_columns() -> None:
    assert len(_TABLES) == 24
    assert set(_TABLES) <= set(Base.metadata.tables)
    assert spec_column_mismatches(_SPEC_COLUMNS) == {}


# (table, column) -> (Literal alias, values in specs/03 §5 order).
_ENUMS: dict[tuple[str, str], tuple[Any, tuple[str, ...]]] = {
    ("voices", "provider"): (VoiceProvider, ("elevenlabs",)),
    ("voices", "service"): (VoiceService, ("ELEVEN_FLASH", "ELEVEN_TURBO", "ELEVEN_MULTILINGUAL")),
    ("voices", "category"): (VoiceCategory, ("premade", "cloned", "generated", "professional", "shared")),
    ("voices", "clone_status"): (VoiceCloneStatus, ("processing", "ready", "failed")),
    ("tools", "method"): (ToolHttpMethod, ("GET", "POST", "PUT", "PATCH", "DELETE")),
    ("knowledge_bases", "status"): (KnowledgeBaseStatus, ("PROCESSING", "COMPLETED", "FAILED")),
    ("kb_documents", "type"): (KbDocumentType, ("FILE", "WEB_SCRAPE", "TEXT")),
    ("kb_documents", "status"): (KnowledgeBaseStatus, ("PROCESSING", "COMPLETED", "FAILED")),
    ("pathway_versions", "status"): (VersionStatus, ("draft", "published", "archived")),
    ("persona_versions", "status"): (VersionStatus, ("draft", "published", "archived")),
    ("pathway_test_runs", "kind"): (PathwayTestRunKind, ("unit", "simulation", "regression")),
    ("pathway_test_runs", "status"): (
        PathwayTestRunStatus,
        ("queued", "running", "passed", "failed", "error"),
    ),
    ("experiments", "status"): (ExperimentStatus, ("draft", "running", "stopped")),
}

# Non-enum CHECKs: constraint name -> SQL text exactly as written in the spec.
_RULE_CHECKS: dict[tuple[str, str], str] = {
    ("voice_ratings", "voice_ratings_rating_check"): "rating BETWEEN 1 AND 5",
    ("tools", "tools_name_check"): "name ~ '^[A-Za-z][A-Za-z0-9_]{0,63}$'",
    ("tools", "tools_timeout_ms_check"): "timeout_ms BETWEEN 100 AND 60000",
    ("tools", "tools_retries_check"): "retries BETWEEN 0 AND 3",
    ("pathway_unit_tests", "pathway_unit_tests_simulated_variations_check"): (
        "simulated_variations BETWEEN 0 AND 20"
    ),
    ("agent_versions", "agent_versions_semver_check"): (
        r"semver ~ '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'"
    ),
    ("agent_versions", "agent_versions_branch_check"): "char_length(branch) BETWEEN 1 AND 64",
    ("experiment_arms", "experiment_arms_weight_check"): "weight BETWEEN 0 AND 100",
    ("dispositions", "dispositions_tag_check"): "tag ~ '^[A-Z][A-Z0-9_]{0,63}$'",
    ("widgets", "widgets_messages_per_minute_check"): "messages_per_minute BETWEEN 1 AND 600",
}


@pytest.mark.parametrize("key", _ENUMS)
def test_enum_checks_mirror_literals(key: tuple[str, str]) -> None:
    assert_enum_check_mirrors(*key, *_ENUMS[key])


@pytest.mark.parametrize("key", _RULE_CHECKS)
def test_rule_checks_match_spec_text(key: tuple[str, str]) -> None:
    check = constraint(*key)
    assert isinstance(check, CheckConstraint)
    assert str(check.sqltext) == _RULE_CHECKS[key]


def test_check_constraints_are_exactly_the_spec_set() -> None:
    enum_names = {f"{table}_{column}_check" for table, column in _ENUMS}
    assert check_names(_TABLES) == enum_names | {name for _, name in _RULE_CHECKS}


_VOICE_SETTINGS = (
    '\'{"stability":0.5,"similarity_boost":0.75,"style":0,"use_speaker_boost":true,"speed":1.0}\'::jsonb'
)
_EMPTY_OBJECT = "'{}'::jsonb"
_EMPTY_LIST = "'[]'::jsonb"


def test_server_defaults() -> None:
    assert server_defaults(_TABLES) == {
        ("prompts", "tags"): "'{}'",
        ("voices", "description"): "''",
        ("voices", "provider"): "'elevenlabs'",
        ("voices", "service"): "'ELEVEN_FLASH'",
        ("voices", "category"): "'premade'",
        ("voices", "is_public"): "false",
        ("voices", "tags"): "'{}'",
        ("voices", "language_codes"): "'{en}'",
        ("voices", "settings"): _VOICE_SETTINGS,
        ("voices", "rating_sum"): "0",
        ("voices", "rating_count"): "0",
        ("tools", "method"): "'POST'",
        ("tools", "headers"): _EMPTY_OBJECT,
        ("tools", "query"): _EMPTY_OBJECT,
        ("tools", "input_schema"): '\'{"type":"object","properties":{}}\'::jsonb',
        ("tools", "response_map"): _EMPTY_OBJECT,
        ("tools", "timeout_ms"): "10000",
        ("tools", "retries"): "0",
        ("knowledge_bases", "description"): "''",
        ("knowledge_bases", "status"): "'PROCESSING'",
        ("knowledge_bases", "chunk_size_tokens"): "400",
        ("knowledge_bases", "chunk_overlap_tokens"): "60",
        ("knowledge_bases", "document_count"): "0",
        ("knowledge_bases", "chunk_count"): "0",
        ("kb_documents", "status"): "'PROCESSING'",
        ("kb_chunks", "heading_path"): "'{}'",
        ("pathways", "description"): "''",
        ("pathways", "draft_version"): "1",
        ("pathways", "latest_version"): "1",
        ("pathway_versions", "nodes"): _EMPTY_LIST,
        ("pathway_versions", "edges"): _EMPTY_LIST,
        ("pathway_versions", "global_config"): _EMPTY_OBJECT,
        ("pathway_unit_tests", "simulated_variations"): "5",
        ("pathway_test_runs", "results"): _EMPTY_LIST,
        ("personas", "description"): "''",
        ("personas", "draft_version"): "1",
        ("personas", "latest_version"): "1",
        ("agents", "description"): "''",
        ("agents", "env_pins"): _EMPTY_OBJECT,
        ("agent_versions", "branch"): "'main'",
        ("agent_versions", "call_settings"): _EMPTY_OBJECT,
        ("agent_versions", "changelog"): "''",
        ("experiments", "status"): "'draft'",
        ("experiments", "primary_metric"): "'disposition_rate'",
        ("experiments", "primary_metric_config"): _EMPTY_OBJECT,
        ("dispositions", "description"): "''",
        ("citation_schemas", "description"): "''",
        ("eval_setups", "description"): "''",
        ("eval_setups", "latest_version"): "1",
        ("eval_setup_versions", "pass_threshold"): "70",
        ("widgets", "allowed_domains"): "'{}'",
        ("widgets", "messages_per_minute"): "20",
        ("widgets", "voice_enabled"): "false",
        ("widgets", "config"): _EMPTY_OBJECT,
    }


_COMPOSITE_UNIQUES = {
    ("voice_ratings_pkey", ("voice_id", "user_id")),
    ("tools_org_id_name_key", ("org_id", "name")),
    ("pathway_versions_pathway_id_version_number_key", ("pathway_id", "version_number")),
    ("persona_versions_persona_id_version_number_key", ("persona_id", "version_number")),
    ("agent_versions_agent_id_semver_key", ("agent_id", "semver")),
    ("dispositions_org_id_tag_key", ("org_id", "tag")),
    ("eval_setup_versions_eval_setup_id_version_number_key", ("eval_setup_id", "version_number")),
    ("widgets_public_key_key", ("public_key",)),
}


def test_unique_and_primary_key_names() -> None:
    assert unique_and_pk(_TABLES) == {
        *((f"{table}_pkey", ("id",)) for table in _TABLES if table != "voice_ratings"),
        *_COMPOSITE_UNIQUES,
    }


# Foreign keys besides each table's ``org_id`` CASCADE: (table, column, target, ondelete).
_OTHER_FKS = {
    ("voice_samples", "voice_id", "voices.id", "CASCADE"),
    ("voice_ratings", "voice_id", "voices.id", "CASCADE"),
    ("voice_ratings", "user_id", "users.id", "CASCADE"),
    ("kb_documents", "kb_id", "knowledge_bases.id", "CASCADE"),
    ("kb_chunks", "kb_id", "knowledge_bases.id", "CASCADE"),
    ("kb_chunks", "document_id", "kb_documents.id", "CASCADE"),
    ("pathway_folders", "parent_id", "pathway_folders.id", "CASCADE"),
    ("pathways", "folder_id", "pathway_folders.id", "SET NULL"),
    ("pathway_versions", "pathway_id", "pathways.id", "CASCADE"),
    ("pathway_versions", "published_by", "users.id", "SET NULL"),
    ("pathway_unit_tests", "pathway_id", "pathways.id", "CASCADE"),
    ("pathway_test_runs", "pathway_id", "pathways.id", "CASCADE"),
    ("persona_versions", "persona_id", "personas.id", "CASCADE"),
    ("agent_versions", "agent_id", "agents.id", "CASCADE"),
    ("agent_versions", "pathway_id", "pathways.id", "RESTRICT"),
    ("agent_versions", "created_by", "users.id", "SET NULL"),
    ("experiments", "agent_id", "agents.id", "CASCADE"),
    ("experiment_arms", "experiment_id", "experiments.id", "CASCADE"),
    ("experiment_arms", "agent_version_id", "agent_versions.id", "RESTRICT"),
    ("eval_setup_versions", "eval_setup_id", "eval_setups.id", "CASCADE"),
    ("widgets", "pathway_id", "pathways.id", "SET NULL"),
    ("widgets", "agent_id", "agents.id", "SET NULL"),
    ("widgets", "persona_id", "personas.id", "SET NULL"),
    ("widgets", "voice_id", "voices.id", "SET NULL"),
}


def test_foreign_keys_ondelete() -> None:
    org_fks = {(table, "org_id", "orgs.id", "CASCADE") for table in _TABLES}
    expected = {(f"{t}_{c}_fkey", t, c, target, ondelete) for t, c, target, ondelete in org_fks | _OTHER_FKS}
    assert foreign_keys(_TABLES) == expected


def test_pathway_unit_test_source_call_id_has_no_foreign_key() -> None:
    assert not get_table("pathway_unit_tests").c.source_call_id.foreign_keys


# Spec indexes; the org_idx of the tables listed in the specs/03 §1 decision is added below.
_SPEC_INDEXES = {
    "CREATE INDEX prompts_org_idx ON prompts (org_id)",
    "CREATE INDEX voices_org_idx ON voices (org_id)",
    "CREATE UNIQUE INDEX voices_name_scope_uq ON voices "
    "(coalesce(org_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(name)) WHERE deleted_at IS NULL",
    "CREATE UNIQUE INDEX voices_provider_uq ON voices "
    "(coalesce(org_id, '00000000-0000-0000-0000-000000000000'::uuid), provider_voice_id) "
    "WHERE deleted_at IS NULL",
    "CREATE INDEX voice_samples_voice_idx ON voice_samples (voice_id)",
    "CREATE INDEX knowledge_bases_org_idx ON knowledge_bases (org_id)",
    "CREATE INDEX kb_documents_kb_idx ON kb_documents (kb_id)",
    "CREATE INDEX kb_chunks_kb_idx ON kb_chunks (kb_id)",
    "CREATE INDEX kb_chunks_embedding_hnsw ON kb_chunks USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64)",
    "CREATE INDEX kb_chunks_tsv_idx ON kb_chunks USING gin (tsv)",
    "CREATE INDEX pathway_folders_org_idx ON pathway_folders (org_id)",
    "CREATE INDEX pathways_org_idx ON pathways (org_id)",
    "CREATE INDEX pathway_versions_org_idx ON pathway_versions (org_id)",
    "CREATE INDEX pathway_unit_tests_pathway_idx ON pathway_unit_tests (pathway_id)",
    "CREATE INDEX pathway_test_runs_pathway_idx ON pathway_test_runs (pathway_id, created_at DESC)",
    "CREATE INDEX personas_org_idx ON personas (org_id)",
    "CREATE INDEX agents_org_idx ON agents (org_id)",
    "CREATE INDEX agent_versions_agent_branch_idx ON agent_versions (agent_id, branch, created_at DESC)",
    "CREATE UNIQUE INDEX experiments_one_running_per_agent ON experiments (agent_id) "
    "WHERE status = 'running'",
    "CREATE INDEX widgets_org_idx ON widgets (org_id)",
}
_DECISION_ORG_INDEXED = (
    "voice_samples",
    "voice_ratings",
    "kb_documents",
    "kb_chunks",
    "pathway_unit_tests",
    "pathway_test_runs",
    "persona_versions",
    "agent_versions",
    "experiments",
    "experiment_arms",
    "citation_schemas",
    "eval_setups",
    "eval_setup_versions",
)


def test_indexes() -> None:
    decision = {f"CREATE INDEX {table}_org_idx ON {table} (org_id)" for table in _DECISION_ORG_INDEXED}
    assert index_ddl(_TABLES) == _SPEC_INDEXES | decision


@pytest.mark.parametrize("table", [table for table in _TABLES if table != "voices"])
def test_tenant_tables_have_org_id_leading_index(table: str) -> None:
    """specs/03 §1: NOT NULL ``org_id`` plus an index (or UNIQUE) that starts with it."""
    assert get_table(table).c.org_id.nullable is False
    assert "org_id" in leading_columns(table)


def test_voices_org_id_is_nullable_and_indexed() -> None:
    assert get_table("voices").c.org_id.nullable is True
    assert "org_id" in leading_columns("voices")


def test_kb_chunk_vector_and_generated_tsvector() -> None:
    embedding = get_table("kb_chunks").c.embedding.type
    assert isinstance(embedding, VECTOR)
    assert embedding.dim == EMBEDDING_DIMENSIONS == 1024
    assert type(get_table("kb_chunks").c.tsv.type) is TSVECTOR
    assert computed_columns(_TABLES) == {("kb_chunks", "tsv"): ("to_tsvector('simple', content)", True)}


@pytest.mark.parametrize(
    ("table", "column", "sql_type"),
    [
        ("voice_ratings", "rating", SmallInteger),
        ("tools", "retries", SmallInteger),
        ("tools", "timeout_ms", Integer),
        ("tools", "secret_headers_enc", LargeBinary),
        ("kb_documents", "size_bytes", BigInteger),
    ],
)
def test_column_types(table: str, column: str, sql_type: type[Any]) -> None:
    assert type(get_table(table).c[column].type) is sql_type


def test_pass_threshold_is_numeric_5_2() -> None:
    pass_threshold = get_table("eval_setup_versions").c.pass_threshold.type
    assert isinstance(pass_threshold, Numeric)
    assert (pass_threshold.precision, pass_threshold.scale, pass_threshold.asdecimal) == (5, 2, True)


def test_text_array_columns() -> None:
    assert text_array_columns(_TABLES) == {
        ("prompts", "tags"),
        ("voices", "tags"),
        ("voices", "language_codes"),
        ("kb_chunks", "heading_path"),
        ("widgets", "allowed_domains"),
    }


def test_every_timestamp_is_timezone_aware() -> None:
    assert naive_timestamps(_TABLES) == []
