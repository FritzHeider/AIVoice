"""Per-org data keys over ``org_data_keys`` (specs/28-security.md §3; specs/03-data-model.md §3).

:class:`OrgDataKeyRepo` reads and writes one org's keys on a ``tenant_session`` (RLS, plus an explicit
``org_id`` filter); :class:`OrgDataKeyStore` is the ``va_common.crypto.DataKeyStore`` the envelope uses,
opening its own tenant session per lookup.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.crypto import NIL_ORG_ID, WrappedDataKey
from va_db.models import OrgDataKey
from va_db.repositories.base import conflict_savepoint
from va_db.session import Database

__all__ = ["OrgDataKeyRepo", "OrgDataKeyStore"]


class OrgDataKeyRepo:
    """One org's wrapped data keys; use it inside ``tenant_session(org_id)`` with the same org."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active(self, org_id: uuid.UUID) -> WrappedDataKey | None:
        """The org's ``active`` wrapped data key, or ``None`` when it has none."""
        statement = select(OrgDataKey.kek_id, OrgDataKey.encrypted_data_key).where(
            OrgDataKey.org_id == org_id, OrgDataKey.status == "active"
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        kek_id, encrypted_data_key = row
        return WrappedDataKey(kek_id=kek_id, encrypted_data_key=encrypted_data_key)

    async def add_active(self, org_id: uuid.UUID, wrapped: WrappedDataKey) -> OrgDataKey:
        """Store ``wrapped`` as the org's active key; ``CONFLICT`` when the org already has one."""
        row = OrgDataKey(
            org_id=org_id,
            kek_id=wrapped.kek_id,
            encrypted_data_key=wrapped.encrypted_data_key,
            status="active",
        )
        async with conflict_savepoint(self.session, "The org already has an active data key."):
            self.session.add(row)
        return row


class OrgDataKeyStore:
    """``DataKeyStore`` reading the active key in a ``tenant_session`` of the org (committed rows only)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def active_data_key(self, org_id: uuid.UUID) -> WrappedDataKey | None:
        """The org's active wrapped data key; ``None`` for the platform org, which has no row."""
        if org_id == NIL_ORG_ID:
            return None
        async with self._database.tenant_session(org_id) as session:
            return await OrgDataKeyRepo(session).active(org_id)
