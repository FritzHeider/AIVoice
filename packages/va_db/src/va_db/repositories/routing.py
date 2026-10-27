"""Inbound call routing lookup across orgs (specs/03-data-model.md §2; specs/06 §7 step 2)."""

from __future__ import annotations

from sqlalchemy import select

from va_db.models import PhoneNumber
from va_db.repositories.base import SystemRepository

__all__ = ["PhoneNumberRoutingRepo"]


class PhoneNumberRoutingRepo(SystemRepository):
    """Finds the org owning a called number before any org context exists (system scope, specs/03 §2)."""

    async def get_active(self, e164: str) -> PhoneNumber | None:
        """The live ``active`` number ``e164``; ``None`` if pending, releasing, released, deleted, unknown."""
        statement = select(PhoneNumber).where(
            PhoneNumber.e164 == e164,
            PhoneNumber.status == "active",
            PhoneNumber.deleted_at.is_(None),
        )
        return await self.session.scalar(statement)
