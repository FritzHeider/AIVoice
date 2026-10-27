"""VoiceAgents Postgres layer (specs/03-data-model.md): engines, tenant/system sessions, base model."""

from __future__ import annotations

from va_db.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin
from va_db.engine import DbRole, create_engine
from va_db.session import Database, configure_database, get_database, system_session, tenant_session

__all__ = [
    "Base",
    "Database",
    "DbRole",
    "IdMixin",
    "SoftDeleteMixin",
    "TimestampMixin",
    "configure_database",
    "create_engine",
    "get_database",
    "system_session",
    "tenant_session",
]
