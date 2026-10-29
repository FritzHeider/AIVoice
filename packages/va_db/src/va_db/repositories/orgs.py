"""Org reads and general updates (specs/04-auth-tenancy-rbac.md §8.1; specs/03-data-model.md §2, §3 ``orgs``).

``orgs`` is a global identity table without ``org_id`` RLS, so :class:`OrgRepo` runs on
``Database.global_session()``. Nothing in the database confines it to one org: callers must pass the org id of
the request context, never an id taken from the request. ``plans`` (system scope, no RLS, readable by
``va_app``) is joined for the effective limits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import Org, Plan

__all__ = ["OrgOverview", "OrgRepo"]


@dataclass(frozen=True, slots=True)
class OrgOverview:
    """An org with its plan (``None`` when ``plan_id`` names no ``plans`` row)."""

    org: Org
    plan: Plan | None


class OrgRepo:
    """Reads and updates ``orgs`` rows; use it inside ``global_session()`` with the context's org id."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, org_id: uuid.UUID) -> Org | None:
        """The org ``org_id``, or ``None``."""
        return await self.session.get(Org, org_id, populate_existing=True)

    async def overview(self, org_id: uuid.UUID) -> OrgOverview | None:
        """The org ``org_id`` and its plan, or ``None`` when the org does not exist."""
        statement = select(Org, Plan).outerjoin(Plan, Plan.id == Org.plan_id).where(Org.id == org_id)
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        org, plan = row
        return OrgOverview(org=org, plan=plan)

    async def update(
        self,
        org_id: uuid.UUID,
        *,
        name: str | None = None,
        retention_days: int | None = None,
        recording_retention_days: int | None = None,
    ) -> Org | None:
        """Set the given columns (``None`` leaves a column unchanged); the updated org or ``None``.

        The ``set_updated_at()`` trigger maintains ``updated_at``; with nothing to set the org is returned
        untouched.
        """
        values: dict[str, Any] = {
            column: value
            for column, value in (
                ("name", name),
                ("retention_days", retention_days),
                ("recording_retention_days", recording_retention_days),
            )
            if value is not None
        }
        if not values:
            return await self.get(org_id)
        statement = (
            update(Org)
            .where(Org.id == org_id)
            .values(**values)
            .returning(Org)
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(statement)
