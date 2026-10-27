"""Reusable library resources: prompts, voices, tools, knowledge bases (specs/03-data-model.md §5).

Follows the conventions of :mod:`va_db.models.tenancy`: explicit SQL types, ``Literal`` aliases enforced by
:func:`check_in`, and spec-named indexes. Per the §1 decision, every tenant table has an ``org_id`` index.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, get_args

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import (
    bool_column,
    fk_column,
    int_column,
    jsonb_column,
    org_id_column,
    platform_scope,
    text_array_column,
    text_column,
)

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "KbChunk",
    "KbDocument",
    "KbDocumentType",
    "KnowledgeBase",
    "KnowledgeBaseStatus",
    "Prompt",
    "Tool",
    "ToolHttpMethod",
    "Voice",
    "VoiceCategory",
    "VoiceCloneStatus",
    "VoiceProvider",
    "VoiceRating",
    "VoiceSample",
    "VoiceService",
]

VoiceProvider = Literal["elevenlabs"]
VoiceService = Literal["ELEVEN_FLASH", "ELEVEN_TURBO", "ELEVEN_MULTILINGUAL"]
VoiceCategory = Literal["premade", "cloned", "generated", "professional", "shared"]
VoiceCloneStatus = Literal["processing", "ready", "failed"]
ToolHttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
# Shared by ``knowledge_bases.status`` and ``kb_documents.status``.
KnowledgeBaseStatus = Literal["PROCESSING", "COMPLETED", "FAILED"]
KbDocumentType = Literal["FILE", "WEB_SCRAPE", "TEXT"]

# Embedding width of ``Qwen/Qwen3-Embedding-8B`` as configured for Telnyx Inference (specs/00 §2).
EMBEDDING_DIMENSIONS = 1024

_VOICE_SETTINGS_DEFAULT = (
    '{"stability":0.5,"similarity_boost":0.75,"style":0,"use_speaker_boost":true,"speed":1.0}'
)


class Prompt(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``prompts``: a reusable prompt text, public id ``PT-<uuid>``."""

    __tablename__ = "prompts"
    __table_args__ = (Index("prompts_org_idx", "org_id"),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = text_array_column("{}")


class Voice(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``voices``: ``org_id`` NULL is a platform/public catalog voice; ``settings`` is a ``VoiceSettings``.

    ``consent`` records clone consent ``{attested_by_user_id, attested_at, statement_version, ip}``. Names and
    provider voice ids are unique per scope (org or platform) among rows that are not soft-deleted.
    """

    __tablename__ = "voices"
    __table_args__ = (
        check_in("provider", get_args(VoiceProvider)),
        check_in("service", get_args(VoiceService)),
        check_in("category", get_args(VoiceCategory)),
        check_in("clone_status", get_args(VoiceCloneStatus)),
        Index("voices_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID | None] = org_id_column(nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")
    provider: Mapped[VoiceProvider] = text_column("elevenlabs")
    provider_voice_id: Mapped[str] = mapped_column(Text, nullable=False)
    service: Mapped[VoiceService] = text_column("ELEVEN_FLASH")
    category: Mapped[VoiceCategory] = text_column("premade")
    is_public: Mapped[bool] = bool_column(default=False)
    tags: Mapped[list[str]] = text_array_column("{}")
    language_codes: Mapped[list[str]] = text_array_column("{en}")
    gender: Mapped[str | None] = mapped_column(Text)
    accent: Mapped[str | None] = mapped_column(Text)
    age: Mapped[str | None] = mapped_column(Text)
    preview_url: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = jsonb_column(_VOICE_SETTINGS_DEFAULT)
    rating_sum: Mapped[int] = int_column(0)
    rating_count: Mapped[int] = int_column(0)
    clone_status: Mapped[VoiceCloneStatus | None] = mapped_column(Text)
    consent: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    clone_error: Mapped[str | None] = mapped_column(Text)


Index(
    "voices_name_scope_uq",
    platform_scope(Voice.org_id),
    func.lower(Voice.name),
    unique=True,
    postgresql_where=Voice.deleted_at.is_(None),
)
Index(
    "voices_provider_uq",
    platform_scope(Voice.org_id),
    Voice.provider_voice_id,
    unique=True,
    postgresql_where=Voice.deleted_at.is_(None),
)


class VoiceSample(IdMixin, TimestampMixin, Base):
    """``voice_samples``: an uploaded audio sample used to clone a voice."""

    __tablename__ = "voice_samples"
    __table_args__ = (
        Index("voice_samples_voice_idx", "voice_id"),
        Index("voice_samples_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    voice_id: Mapped[uuid.UUID] = fk_column("voices.id", "CASCADE", nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    transcript: Mapped[str | None] = mapped_column(Text)
    provider_sample_id: Mapped[str | None] = mapped_column(Text)


class VoiceRating(TimestampMixin, Base):
    """``voice_ratings``: one 1-5 rating per user and voice, keyed by the pair of ids."""

    __tablename__ = "voice_ratings"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_check"),
        Index("voice_ratings_org_idx", "org_id"),
    )

    voice_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("voices.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[uuid.UUID] = org_id_column()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)


class Tool(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``tools``: an HTTP tool the agent can call, public id ``TL-<uuid>`` (specs/13).

    ``headers`` values may contain ``{{vars}}``; secret header values are encrypted in ``secret_headers_enc``.
    ``input_schema`` is a JSON Schema object and ``response_map`` maps ``{"var_name": "$.json.path"}``.
    """

    __tablename__ = "tools"
    __table_args__ = (
        CheckConstraint("name ~ '^[A-Za-z][A-Za-z0-9_]{0,63}$'", name="name_check"),
        check_in("method", get_args(ToolHttpMethod)),
        CheckConstraint("timeout_ms BETWEEN 100 AND 60000", name="timeout_ms_check"),
        CheckConstraint("retries BETWEEN 0 AND 3", name="retries_check"),
        UniqueConstraint("org_id", "name"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Said while the tool runs.
    speech: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[ToolHttpMethod] = text_column("POST")
    headers: Mapped[dict[str, Any]] = jsonb_column("{}")
    secret_headers_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    query: Mapped[dict[str, Any]] = jsonb_column("{}")
    # Any JSON value or NULL (no body).
    body: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    input_schema: Mapped[dict[str, Any]] = jsonb_column('{"type":"object","properties":{}}')
    response_map: Mapped[dict[str, Any]] = jsonb_column("{}")
    timeout_ms: Mapped[int] = int_column(10000)
    retries: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))


class KnowledgeBase(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``knowledge_bases``: a retrieval corpus, public id ``KB-<uuid>``."""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        check_in("status", get_args(KnowledgeBaseStatus)),
        Index("knowledge_bases_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")
    status: Mapped[KnowledgeBaseStatus] = text_column("PROCESSING")
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_size_tokens: Mapped[int] = int_column(400)
    chunk_overlap_tokens: Mapped[int] = int_column(60)
    document_count: Mapped[int] = int_column(0)
    chunk_count: Mapped[int] = int_column(0)


class KbDocument(IdMixin, TimestampMixin, Base):
    """``kb_documents``: one uploaded file, scraped page, or text snippet of a knowledge base."""

    __tablename__ = "kb_documents"
    __table_args__ = (
        check_in("type", get_args(KbDocumentType)),
        check_in("status", get_args(KnowledgeBaseStatus)),
        Index("kb_documents_kb_idx", "kb_id"),
        Index("kb_documents_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    kb_id: Mapped[uuid.UUID] = fk_column("knowledge_bases.id", "CASCADE", nullable=False)
    type: Mapped[KbDocumentType] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    s3_key: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_sha256: Mapped[str | None] = mapped_column(Text)
    status: Mapped[KnowledgeBaseStatus] = text_column("PROCESSING")
    error: Mapped[str | None] = mapped_column(Text)
    token_count: Mapped[int | None] = mapped_column(Integer)


class KbChunk(IdMixin, TimestampMixin, Base):
    """``kb_chunks``: an embedded passage; ``tsv`` is generated by Postgres for keyword search."""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        Index("kb_chunks_kb_idx", "kb_id"),
        Index("kb_chunks_org_idx", "org_id"),
        Index(
            "kb_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("kb_chunks_tsv_idx", "tsv", postgresql_using="gin"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    kb_id: Mapped[uuid.UUID] = fk_column("knowledge_bases.id", "CASCADE", nullable=False)
    document_id: Mapped[uuid.UUID] = fk_column("kb_documents.id", "CASCADE", nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[list[str]] = text_array_column("{}")
    source_url: Mapped[str | None] = mapped_column(Text)
    page_number: Mapped[int | None] = mapped_column(Integer)
    # pgvector's bind processor accepts lists (and numpy arrays) and its result processor parses the
    # text form back into ``list[float]``, so the list type is exact for both directions.
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    # Read-only: Postgres computes it, and ``str`` is the asyncpg text form of a tsvector.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True)
    )
