"""Tables merged from module specs: LLM overrides, DNC imports, erasure requests, org secrets, data keys
(specs/03-data-model.md §9a).

Credentials and secrets are stored envelope-encrypted under the org's active data key (``org_data_keys``,
specs/28-security); ``org_secrets`` are referenced by tools and dynamic data as ``{{secrets.NAME}}``
(specs/04 §7). Follows the conventions of :mod:`va_db.models.tenancy`; per the §1 decision, every tenant table
has an ``org_id`` index.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, get_args

from sqlalchemy import CheckConstraint, Index, Integer, LargeBinary, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import bool_column, jsonb_column, org_fk, org_id_column, tz

__all__ = [
    "DncImport",
    "ErasureRequest",
    "JobStatus",
    "OrgDataKey",
    "OrgDataKeyStatus",
    "OrgLlmProvider",
    "OrgSecret",
]

# Shared by ``dnc_imports.status`` and ``erasure_requests.status``.
JobStatus = Literal["queued", "running", "completed", "failed"]
OrgDataKeyStatus = Literal["active", "retired"]


class OrgLlmProvider(IdMixin, TimestampMixin, Base):
    """``org_llm_providers``: an org's OpenAI-compatible LLM override (specs/10-llm-gateway), at most one per
    org.

    ``models`` is ``{"dialogue": ..., "router": ..., "analysis": ..., "fallback": ...}``.
    """

    __tablename__ = "org_llm_providers"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), org_fk(), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    models: Mapped[dict[str, Any]] = jsonb_column()
    supports_tool_choice: Mapped[bool] = bool_column(default=True)
    baa_covered: Mapped[bool] = bool_column(default=False)
    last_probe_at: Mapped[datetime | None] = mapped_column(tz())
    last_probe_error: Mapped[str | None] = mapped_column(Text)


class DncImport(IdMixin, TimestampMixin, Base):
    """``dnc_imports``: an asynchronous bulk DNC CSV import (specs/23-guardrails-and-compliance); the counters
    are filled in when the import finishes.
    """

    __tablename__ = "dnc_imports"
    __table_args__ = (check_in("status", get_args(JobStatus)),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    status: Mapped[JobStatus] = mapped_column(Text, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int | None] = mapped_column(Integer)
    inserted: Mapped[int | None] = mapped_column(Integer)
    invalid: Mapped[int | None] = mapped_column(Integer)
    duplicates: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


Index("dnc_imports_org_idx", DncImport.org_id, DncImport.created_at.desc())


class ErasureRequest(IdMixin, TimestampMixin, Base):
    """``erasure_requests``: right-to-erasure tracking (specs/28-security).

    ``selector`` is one of ``{"contact_id"|"phone_e164"|"email"|"external_id": ...}``.
    """

    __tablename__ = "erasure_requests"
    __table_args__ = (check_in("status", get_args(JobStatus)),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    selector: Mapped[dict[str, Any]] = jsonb_column()
    status: Mapped[JobStatus] = mapped_column(Text, nullable=False)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)


Index("erasure_requests_org_idx", ErasureRequest.org_id, ErasureRequest.created_at.desc())


class OrgSecret(IdMixin, TimestampMixin, Base):
    """``org_secrets``: a named, encrypted secret; ``name`` is an upper-case identifier of at most 64
    characters, unique per org.
    """

    __tablename__ = "org_secrets"
    __table_args__ = (
        CheckConstraint("name ~ '^[A-Z][A-Z0-9_]{0,63}$'", name="name_check"),
        UniqueConstraint("org_id", "name"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class OrgDataKey(IdMixin, TimestampMixin, Base):
    """``org_data_keys``: an org data key wrapped by the KEK ``kek_id``; at most one is active per org."""

    __tablename__ = "org_data_keys"
    __table_args__ = (
        check_in("status", get_args(OrgDataKeyStatus)),
        Index("org_data_keys_active_uq", "org_id", unique=True, postgresql_where=text("status = 'active'")),
        Index("org_data_keys_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    kek_id: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_data_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[OrgDataKeyStatus] = mapped_column(Text, nullable=False)
