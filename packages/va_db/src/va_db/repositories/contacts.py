"""Tenant ``contacts`` writes (specs/03-data-model.md §6): the contact of an inbound caller (specs/06 §7
Decision P05-T12c)."""

from __future__ import annotations

import uuid

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.ids import new_id
from va_db.models import Contact

__all__ = ["ContactRepo"]


class ContactRepo:
    """One org's contacts inside the caller's ``tenant_session(org_id)``."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self._session = session
        self._org_id = org_id

    async def upsert_phone(self, phone_e164: str) -> uuid.UUID:
        """The id of the org's contact with ``phone_e164``, created when there is none (``updated_at`` is
        touched otherwise). Concurrent calls from the same number converge on one row
        (``contacts_phone_uq``)."""
        statement = (
            insert(Contact)
            .values(id=new_id(), org_id=self._org_id, phone_e164=phone_e164)
            .on_conflict_do_update(
                index_elements=[Contact.org_id, Contact.phone_e164],
                index_where=text("phone_e164 IS NOT NULL"),
                set_={"updated_at": func.now()},
            )
            .returning(Contact.id)
        )
        contact_id: uuid.UUID = (await self._session.execute(statement)).scalar_one()
        return contact_id
