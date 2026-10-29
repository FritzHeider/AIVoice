"""Org memberships (specs/04-auth-tenancy-rbac.md §4.1, §6; specs/03-data-model.md §2, §3).

:class:`MembershipRepo` reads and adds one org's memberships on a ``tenant_session`` (RLS plus an explicit
``org_id`` filter). :class:`MembershipLookupRepo` lists a user's memberships across orgs before an active org
exists, so it is a ``va_system`` repository named in specs/03 §2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import MemberRole, Membership, Org, OrgStatus
from va_db.repositories.base import SystemRepository, conflict_savepoint

__all__ = ["MembershipLookupRepo", "MembershipRecord", "MembershipRepo"]


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    """One of a user's memberships joined with the org fields the dashboard session needs."""

    membership_id: uuid.UUID
    role: MemberRole
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    plan_id: str
    org_status: OrgStatus
    parent_org_id: uuid.UUID | None


class MembershipLookupRepo(SystemRepository):
    """A user's memberships in every org (system scope, specs/03-data-model.md §2)."""

    async def for_user(self, user_id: uuid.UUID) -> list[MembershipRecord]:
        """The user's memberships, oldest first (membership ``created_at``, then ``id``)."""
        statement = (
            select(Membership, Org)
            .join(Org, Org.id == Membership.org_id)
            .where(Membership.user_id == user_id)
            .order_by(Membership.created_at, Membership.id)
        )
        rows = (await self.session.execute(statement)).all()
        records: list[MembershipRecord] = []
        for membership, org in rows:
            records.append(
                MembershipRecord(
                    membership_id=membership.id,
                    role=membership.role,
                    org_id=org.id,
                    org_name=org.name,
                    org_slug=org.slug,
                    plan_id=org.plan_id,
                    org_status=org.status,
                    parent_org_id=org.parent_org_id,
                )
            )
        return records


class MembershipRepo:
    """Memberships of one org; use it inside ``tenant_session(org_id)`` with the same org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    async def add(self, user_id: uuid.UUID, role: MemberRole) -> Membership:
        """Make ``user_id`` a member with ``role``; ``CONFLICT`` when already a member."""
        membership = Membership(org_id=self.org_id, user_id=user_id, role=role)
        async with conflict_savepoint(self.session, "User is already a member of this org."):
            self.session.add(membership)
        await self.session.refresh(membership)
        return membership

    async def get_for_user(self, user_id: uuid.UUID) -> Membership | None:
        """The user's membership in this org, or ``None``."""
        statement = select(Membership).where(Membership.org_id == self.org_id, Membership.user_id == user_id)
        return await self.session.scalar(statement)
