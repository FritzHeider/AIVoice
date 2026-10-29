"""Org memberships (specs/04-auth-tenancy-rbac.md §4.1, §6; specs/03-data-model.md §2, §3).

:class:`MembershipRepo` reads, adds, re-roles and removes one org's memberships on a ``tenant_session`` (RLS
plus an explicit ``org_id`` filter). Every change that can take away an owner first locks the org row
(``SELECT … FOR UPDATE``) and only then counts the owners, so concurrent demotions are serialised and an org
always keeps at least one owner. :class:`MembershipLookupRepo` lists a user's memberships across orgs before
an active org exists, so it is a ``va_system`` repository named in specs/03 §2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_db.models import MemberRole, Membership, Org, OrgStatus, User
from va_db.repositories.api_keys import rate_per_sec_override
from va_db.repositories.base import SystemRepository, conflict_savepoint

__all__ = [
    "LAST_OWNER_MESSAGE",
    "NOT_OWNER_MESSAGE",
    "MemberRecord",
    "MembershipLookupRepo",
    "MembershipRecord",
    "MembershipRepo",
]


MFA_REQUIRED_KEY = "mfa_required"
LAST_OWNER_MESSAGE: Final = "An organization must have at least one owner."
NOT_OWNER_MESSAGE: Final = "Only an owner can transfer ownership."


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


@dataclass(frozen=True, slots=True)
class MemberRecord:
    """A member of the org as ``GET /v1/org/members`` lists it; ``joined_at`` is the membership's creation."""

    user_id: uuid.UUID
    email: str
    name: str
    role: MemberRole
    mfa_enabled: bool
    last_login_at: datetime | None
    joined_at: datetime


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

    def _members(self) -> Select[uuid.UUID, str, str, MemberRole, bool, datetime | None, datetime]:
        return (
            select(
                Membership.user_id,
                User.email,
                User.name,
                Membership.role,
                User.mfa_enabled,
                User.last_login_at,
                Membership.created_at,
            )
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == self.org_id)
        )

    async def list_members(self) -> list[MemberRecord]:
        """Every member, oldest membership first (then ``user_id``)."""
        statement = self._members().order_by(Membership.created_at, Membership.user_id)
        return [MemberRecord(*row) for row in (await self.session.execute(statement)).all()]

    async def member(self, user_id: uuid.UUID) -> MemberRecord | None:
        """The member ``user_id`` of this org, or ``None`` (also for members of other orgs)."""
        row = (await self.session.execute(self._members().where(Membership.user_id == user_id))).first()
        return None if row is None else MemberRecord(*row)

    async def member_by_email(self, email: str) -> MemberRecord | None:
        """The member whose email is ``email`` (case-insensitive ``citext``), or ``None``."""
        row = (await self.session.execute(self._members().where(User.email == email.strip()))).first()
        return None if row is None else MemberRecord(*row)

    async def lock_org(self) -> None:
        """Lock the org row until the transaction ends; serialises changes to the org's owners."""
        await self.session.execute(select(Org.id).where(Org.id == self.org_id).with_for_update())

    async def owner_count(self) -> int:
        """The number of owners (count after :meth:`lock_org` to see committed concurrent changes)."""
        statement = select(func.count()).where(Membership.org_id == self.org_id, Membership.role == "owner")
        return (await self.session.execute(statement)).scalar_one()

    async def set_role(self, user_id: uuid.UUID, role: MemberRole) -> bool:
        """Give ``user_id`` the ``role`` without any owner check; whether a membership was updated."""
        statement = (
            update(Membership)
            .where(Membership.org_id == self.org_id, Membership.user_id == user_id)
            .values(role=role)
            .returning(Membership.id)
        )
        return await self.session.scalar(statement) is not None

    async def remove(self, user_id: uuid.UUID) -> bool:
        """Delete the membership of ``user_id`` without any owner check; whether one was deleted."""
        statement = (
            delete(Membership)
            .where(Membership.org_id == self.org_id, Membership.user_id == user_id)
            .returning(Membership.id)
        )
        return await self.session.scalar(statement) is not None

    async def _keep_an_owner(self, target: MemberRecord, new_role: MemberRole | None) -> None:
        """``CONFLICT`` when demoting or removing (``new_role`` ``None``) ``target`` leaves no owner."""
        if target.role == "owner" and new_role != "owner" and await self.owner_count() <= 1:
            raise AppError("CONFLICT", LAST_OWNER_MESSAGE)

    async def change_role(self, user_id: uuid.UUID, role: MemberRole) -> MemberRecord | None:
        """Give the member ``role`` under the org-row lock (specs/04 §6); ``None`` when not a member.

        ``CONFLICT`` when it would demote the last owner; the same role is a no-op.
        """
        await self.lock_org()
        target = await self.member(user_id)
        if target is None:
            return None
        await self._keep_an_owner(target, role)
        if target.role != role:
            await self.set_role(user_id, role)
        return await self.member(user_id)

    async def remove_member(self, user_id: uuid.UUID) -> bool:
        """Remove the member under the org-row lock (specs/04 §6); ``False`` when not a member.

        ``CONFLICT`` when it would remove the last owner.
        """
        await self.lock_org()
        target = await self.member(user_id)
        if target is None:
            return False
        await self._keep_an_owner(target, None)
        return await self.remove(user_id)

    async def transfer_ownership(
        self, from_user_id: uuid.UUID, to_user_id: uuid.UUID
    ) -> tuple[MemberRecord, MemberRecord] | None:
        """Make ``to_user_id`` an owner and ``from_user_id`` an admin under the org-row lock (specs/04 §6).

        Returns the previous owner and the new owner, or ``None`` when either is not a member; ``FORBIDDEN``
        when ``from_user_id`` is no longer an owner. Other owners keep their role.
        """
        await self.lock_org()
        previous, target = await self.member(from_user_id), await self.member(to_user_id)
        if previous is None or target is None:
            return None
        if previous.role != "owner":
            raise AppError("FORBIDDEN", NOT_OWNER_MESSAGE)
        await self.set_role(to_user_id, "owner")
        await self.set_role(from_user_id, "admin")
        demoted, promoted = await self.member(from_user_id), await self.member(to_user_id)
        if demoted is None or promoted is None:
            return None
        return demoted, promoted
