"""Org memberships (specs/04-auth-tenancy-rbac.md §4.1, §6; specs/03-data-model.md §2, §3).

:class:`MembershipRepo` reads and adds one org's memberships on a ``tenant_session`` (RLS plus an explicit
``org_id`` filter). :class:`MembershipLookupRepo` lists a user's memberships across orgs before an active org
exists, so it is a ``va_system`` repository named in specs/03 §2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import MemberRole, Membership, Org, OrgStatus, User
from va_db.repositories.api_keys import rate_per_sec_override
from va_db.repositories.base import SystemRepository, conflict_savepoint

__all__ = ["MembershipLookupRepo", "MembershipRecord", "MembershipRepo"]


MFA_REQUIRED_KEY = "mfa_required"


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    """One of a user's memberships joined with the org fields the dashboard session needs.

    ``mfa_required`` is ``orgs.settings.mfa_required`` (only a JSON ``true`` counts); ``rate_per_sec`` is the
    org's positive ``limits.rate_per_sec`` override or ``None``; ``user_mfa_enabled`` is the member's own
    ``users.mfa_enabled``.
    """

    membership_id: uuid.UUID
    role: MemberRole
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    plan_id: str
    org_status: OrgStatus
    parent_org_id: uuid.UUID | None
    mfa_required: bool = False
    rate_per_sec: int | None = None
    user_mfa_enabled: bool = False


def _record(membership: Membership, org: Org, user_mfa_enabled: bool) -> MembershipRecord:
    return MembershipRecord(
        membership_id=membership.id,
        role=membership.role,
        org_id=org.id,
        org_name=org.name,
        org_slug=org.slug,
        plan_id=org.plan_id,
        org_status=org.status,
        parent_org_id=org.parent_org_id,
        mfa_required=org.settings.get(MFA_REQUIRED_KEY) is True,
        rate_per_sec=rate_per_sec_override(org.limits),
        user_mfa_enabled=user_mfa_enabled,
    )


def _joined(user_id: uuid.UUID) -> Select[Membership, Org, bool]:
    return (
        select(Membership, Org, User.mfa_enabled)
        .join(Org, Org.id == Membership.org_id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.user_id == user_id)
    )


class MembershipLookupRepo(SystemRepository):
    """A user's memberships in every org (system scope, specs/03-data-model.md §2)."""

    async def for_user(self, user_id: uuid.UUID) -> list[MembershipRecord]:
        """The user's memberships, oldest first (membership ``created_at``, then ``id``)."""
        statement = _joined(user_id).order_by(Membership.created_at, Membership.id)
        rows = (await self.session.execute(statement)).all()
        return [_record(membership, org, mfa_enabled) for membership, org, mfa_enabled in rows]

    async def for_user_in_org(self, user_id: uuid.UUID, org_id: uuid.UUID) -> MembershipRecord | None:
        """The user's membership in ``org_id``, or ``None``."""
        row = (await self.session.execute(_joined(user_id).where(Membership.org_id == org_id))).first()
        if row is None:
            return None
        membership, org, mfa_enabled = row
        return _record(membership, org, mfa_enabled)


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
