"""Declarative base and column mixins shared by every model (specs/03-data-model.md §1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import DateTime, FetchedValue, MetaData, Uuid, func
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from va_common.ids import new_id

__all__ = ["NAMING_CONVENTION", "Base", "IdMixin", "SoftDeleteMixin", "TimestampMixin"]

# Postgres' own default constraint and index names, so models and the hand-written migration agree.
NAMING_CONVENTION: Final = {
    "pk": "%(table_name)s_pkey",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s",
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
}


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base of every VoiceAgents model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IdMixin:
    """``id uuid PRIMARY KEY`` generated in Python as UUIDv7 (no database default); first column."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id, sort_order=-1)


class TimestampMixin:
    """``created_at``/``updated_at timestamptz NOT NULL DEFAULT now()``; ``updated_at`` via trigger."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), server_onupdate=FetchedValue()
    )


class SoftDeleteMixin:
    """``deleted_at timestamptz`` on user-facing configuration resources."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
