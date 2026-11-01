"""Do-not-call lookups at dispatch (specs/23-guardrails-and-compliance.md §2.1).

A destination is blocked by an unexpired `dnc_entries` row of the platform (`org_id IS NULL`), of the calling
org, or, when the **parent** org's `compliance.dnc_inherit_from_parent` is true, of the direct parent org.
Everything runs in the caller's tenant session: platform rows are visible through RLS, and the parent's rows
are read with `va_db.session.use_org`, which switches `app.org_id` for the lookup and restores it afterwards.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import DncEntry, Org
from va_db.session import use_org
from va_domain.org import load_settings

__all__ = ["DncBlock", "DncScope", "find_block"]

type DncScope = Literal["platform", "org", "parent"]


@dataclass(frozen=True, slots=True)
class DncBlock:
    """Why a destination is blocked: which list matched and the entry's `source`."""

    scope: DncScope
    source: str


def _unexpired(now: datetime) -> ColumnElement[bool]:
    return or_(DncEntry.expires_at.is_(None), DncEntry.expires_at > now)


async def _own_or_platform(
    session: AsyncSession, org_id: uuid.UUID, e164: str, now: datetime
) -> DncBlock | None:
    rows = await session.execute(
        select(DncEntry.org_id, DncEntry.source).where(
            DncEntry.phone_e164 == e164,
            or_(DncEntry.org_id == org_id, DncEntry.org_id.is_(None)),
            _unexpired(now),
        )
    )
    blocks = [DncBlock("platform" if owner is None else "org", source) for owner, source in rows]
    # The platform list wins the label when both match.
    return min(blocks, key=lambda block: block.scope != "platform", default=None)


async def _inherited_parent(session: AsyncSession, org_id: uuid.UUID) -> uuid.UUID | None:
    parent_id = await session.scalar(select(Org.parent_org_id).where(Org.id == org_id))
    if parent_id is None:
        return None
    stored = await session.scalar(select(Org.settings).where(Org.id == parent_id))
    if stored is None or not load_settings(stored).compliance.dnc_inherit_from_parent:
        return None
    return parent_id


async def find_block(
    session: AsyncSession, org_id: uuid.UUID, e164: str, *, now: datetime
) -> DncBlock | None:
    """The DNC entry that blocks `org_id` from calling `e164`, or `None` when the number may be called."""
    block = await _own_or_platform(session, org_id, e164, now)
    if block is not None:
        return block
    parent_id = await _inherited_parent(session, org_id)
    if parent_id is None:
        return None
    async with use_org(session, parent_id):
        source = await session.scalar(
            select(DncEntry.source)
            .where(DncEntry.phone_e164 == e164, DncEntry.org_id == parent_id, _unexpired(now))
            .limit(1)
        )
    return None if source is None else DncBlock("parent", source)
