"""Org and DNC rows for the va_compliance DB tests (each test uses fresh orgs and random numbers)."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Any

from model_factories import make_org

from va_db.models import DncEntry
from va_db.session import Database


def random_e164() -> str:
    """A random New York number (`+1212` and seven random digits, so tests never share a number)."""
    return f"+1212{secrets.randbelow(10**7):07d}"


async def new_org(
    database: Database, *, parent: uuid.UUID | None = None, settings: dict[str, Any] | None = None
) -> uuid.UUID:
    async with database.system_session() as session:
        org = await make_org(session, parent_org_id=parent, settings=settings or {})
        return org.id


async def add_dnc(
    database: Database,
    org_id: uuid.UUID | None,
    e164: str,
    *,
    source: str = "api",
    expires_at: datetime | None = None,
) -> None:
    """Insert a DNC row (`org_id` None = platform row, which only the system role can write)."""
    async with database.system_session() as session:
        session.add(DncEntry(org_id=org_id, phone_e164=e164, source=source, expires_at=expires_at))
