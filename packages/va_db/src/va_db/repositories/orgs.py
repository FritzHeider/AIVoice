"""Org reads and general updates (specs/04-auth-tenancy-rbac.md §8.1; specs/03-data-model.md §2, §3 ``orgs``).

``orgs`` is a global identity table without ``org_id`` RLS, so :class:`OrgRepo` runs on
``Database.global_session()``. Nothing in the database confines it to one org: callers must pass the org id of
the request context, never an id taken from the request. ``plans`` (system scope, no RLS, readable by
``va_app``) is joined for the effective limits. Sub-account reads (specs/04 §8.2) take the caller's org as the
parent and match ``parent_org_id`` against it, so an id from the request only ever finds a direct child.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import Org, OrgStatus, Plan

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

    async def lock(self, org_id: uuid.UUID) -> Org | None:
        """The org ``org_id`` locked ``FOR UPDATE`` until the transaction ends, or ``None``.

        Read-merge-write updates of ``orgs.settings`` take this lock first so concurrent writers never lose
        each other's keys.
        """
        statement = (
            select(Org).where(Org.id == org_id).with_for_update().execution_options(populate_existing=True)
        )
        return await self.session.scalar(statement)

    async def update(
        self,
        org_id: uuid.UUID,
        *,
        name: str | None = None,
        retention_days: int | None = None,
        recording_retention_days: int | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> Org | None:
        """Set the given columns (``None`` leaves a column unchanged); the updated org or ``None``.

        ``settings`` replaces the whole ``orgs.settings`` object (callers pass the merged sparse settings).
        The ``set_updated_at()`` trigger maintains ``updated_at``; with nothing to set the org is returned
        untouched.
        """
        values: dict[str, Any] = {
            column: value
            for column, value in (
                ("name", name),
                ("retention_days", retention_days),
                ("recording_retention_days", recording_retention_days),
                ("settings", None if settings is None else dict(settings)),
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

    async def children(self, parent_id: uuid.UUID) -> list[Org]:
        """The direct sub-accounts of ``parent_id``, oldest first (``created_at``, then ``id``)."""
        statement = (
            select(Org)
            .where(Org.parent_org_id == parent_id)
            .order_by(Org.created_at, Org.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.scalars(statement)).all())

    async def child(self, parent_id: uuid.UUID, child_id: uuid.UUID) -> Org | None:
        """The org ``child_id`` when it is a direct sub-account of ``parent_id``, else ``None``."""
        statement = (
            select(Org)
            .where(Org.id == child_id, Org.parent_org_id == parent_id)
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(statement)

    async def set_status(self, org_id: uuid.UUID, status: OrgStatus) -> Org | None:
        """Set the org's ``status``; the updated org, or ``None`` when it does not exist."""
        statement = (
            update(Org)
            .where(Org.id == org_id)
            .values(status=status)
            .returning(Org)
            .execution_options(populate_existing=True)
        )
        return await self.session.scalar(statement)
