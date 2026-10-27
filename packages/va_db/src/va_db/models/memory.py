"""Memory: per-contact memories and their change log (specs/03-data-model.md §7).

Follows the conventions of :mod:`va_db.models.tenancy`: explicit SQL types, ``Literal`` aliases enforced by
:func:`check_in`, and spec-named indexes. Per the §1 decision, every tenant table has an ``org_id`` index.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, get_args

from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import (
    bool_column,
    fk_column,
    jsonb_column,
    org_id_column,
    platform_scope,
    text_column,
)
from va_db.models.library import EMBEDDING_DIMENSIONS

__all__ = ["Memory", "MemoryChange", "MemoryChangeType"]

MemoryChangeType = Literal["summary", "fact_set", "fact_delete", "open_item_add", "open_item_close", "reset"]


class Memory(IdMixin, TimestampMixin, Base):
    """``memories``: what the platform remembers about a contact, per persona or org-wide.

    ``persona_id`` NULL is the org-wide memory for the contact. ``facts`` is
    ``{"key": {"value": ..., "updated_at": ..., "source_call_id": ...}}``, ``open_items`` is
    ``[{id, text, created_at, due_at?, status}]``, and ``recent_messages`` holds the last 30 messages across
    channels as ``[{channel, role, text, at, call_id|conversation_id}]``.
    """

    __tablename__ = "memories"

    org_id: Mapped[uuid.UUID] = org_id_column()
    contact_id: Mapped[uuid.UUID] = fk_column("contacts.id", "CASCADE", nullable=False)
    persona_id: Mapped[uuid.UUID | None] = fk_column("personas.id", "CASCADE")
    summary: Mapped[str] = text_column("")
    facts: Mapped[dict[str, Any]] = jsonb_column("{}")
    open_items: Mapped[list[Any]] = jsonb_column("[]")
    recent_messages: Mapped[list[Any]] = jsonb_column("[]")
    enabled: Mapped[bool] = bool_column(default=True)
    summary_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))


Index(
    "memories_contact_persona_uq",
    Memory.org_id,
    Memory.contact_id,
    platform_scope(Memory.persona_id),
    unique=True,
)


class MemoryChange(IdMixin, TimestampMixin, Base):
    """``memory_changes``: one audited change to a memory, with the affected value ``before``/``after``.

    ``call_id`` and ``conversation_id`` carry no foreign key, as in the spec, so the audit trail outlives
    the call or conversation that caused the change.
    """

    __tablename__ = "memory_changes"
    __table_args__ = (
        check_in("change_type", get_args(MemoryChangeType)),
        Index("memory_changes_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    memory_id: Mapped[uuid.UUID] = fk_column("memories.id", "CASCADE", nullable=False)
    call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    change_type: Mapped[MemoryChangeType] = mapped_column(Text, nullable=False)
    before: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[Any | None] = mapped_column(JSONB, nullable=True)


Index("memory_changes_memory_idx", MemoryChange.memory_id, MemoryChange.created_at.desc())
