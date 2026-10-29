"""Org invitations (specs/04-auth-tenancy-rbac.md §6; specs/03-data-model.md §2, §3 ``invitations``).

:class:`InvitationRepo` creates, lists, revokes, and consumes one org's invitations on a ``tenant_session``
(RLS plus an explicit ``org_id`` filter). An invitation token is opaque (``va_common.auth.issue_token``);
only its ``sha256_hex`` is stored and the plaintext goes out once, by email. Revoking deletes the row (the
table has no revoked column). :class:`InvitationLookupRepo` resolves a token before any org context exists
(the invitation details and accept endpoints know only the token), so it is a ``va_system`` repository named
in specs/03 §2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import ColumnElement, Text, cast, delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from va_common.auth import issue_token, sha256_hex
from va_db.models import Invitation, MemberRole, Org, OrgStatus, User
from va_db.repositories.base import SystemRepository

__all__ = ["INVITATION_TTL", "InvitationLookup", "InvitationLookupRepo", "InvitationRepo"]

INVITATION_TTL: Final = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class InvitationLookup:
    """An invitation found by its token, joined with its org, its inviter (name, or email when the name is
    empty), and whether the email has an account."""

    id: uuid.UUID
    org_id: uuid.UUID
    org_name: str
    org_status: OrgStatus
    email: str
    role: MemberRole
    expires_at: datetime
    accepted_at: datetime | None
    inviter_name: str
    account_exists: bool

    def usable(self, now: datetime) -> bool:
        """Whether the invitation can still be accepted at ``now`` (not accepted, not expired)."""
        return self.accepted_at is None and self.expires_at > now


class InvitationLookupRepo(SystemRepository):
    """Invitation token → invitation across orgs (system scope, specs/03-data-model.md §2)."""

    async def by_token(self, token: str) -> InvitationLookup | None:
        """The invitation whose plaintext token is ``token`` (accepted or expired too), or ``None``."""
        inviter = aliased(User)
        account = exists().where(User.email == Invitation.email)
        inviter_name = func.coalesce(func.nullif(inviter.name, ""), cast(inviter.email, Text))
        statement = (
            select(Invitation, Org.name, Org.status, inviter_name, account)
            .join(Org, Org.id == Invitation.org_id)
            .join(inviter, inviter.id == Invitation.invited_by)
            .where(Invitation.token_hash == sha256_hex(token))
        )
        row = (await self.session.execute(statement)).first()
        if row is None:
            return None
        invitation, org_name, org_status, inviter_name, account_exists = row
        return InvitationLookup(
            id=invitation.id,
            org_id=invitation.org_id,
            org_name=org_name,
            org_status=org_status,
            email=invitation.email,
            role=invitation.role,
            expires_at=invitation.expires_at,
            accepted_at=invitation.accepted_at,
            inviter_name=inviter_name,
            account_exists=bool(account_exists),
        )


class InvitationRepo:
    """Invitations of one org; use it inside ``tenant_session(org_id)`` with the same org."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self.session = session
        self.org_id = org_id

    def _open(self) -> tuple[ColumnElement[bool], ...]:
        return Invitation.org_id == self.org_id, Invitation.accepted_at.is_(None)

    async def create(
        self, *, email: str, role: MemberRole, invited_by: uuid.UUID, now: datetime
    ) -> tuple[Invitation, str]:
        """Invite ``email`` with ``role``, valid for 7 days from ``now``; the row and the plaintext token.

        An open (unaccepted) invitation of the same email in this org is deleted first, so re-inviting resends
        with a fresh token and the new role.
        """
        await self.session.execute(delete(Invitation).where(*self._open(), Invitation.email == email))
        issued = issue_token()
        row = Invitation(
            org_id=self.org_id,
            email=email,
            role=role,
            token_hash=issued.token_hash,
            invited_by=invited_by,
            expires_at=now + INVITATION_TTL,
            created_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row, issued.plaintext

    async def list_open(self) -> list[Invitation]:
        """Every unaccepted invitation (expired ones too), newest first (``created_at``, then ``id``)."""
        statement = (
            select(Invitation)
            .where(*self._open())
            .order_by(Invitation.created_at.desc(), Invitation.id.desc())
        )
        return list((await self.session.scalars(statement)).all())

    async def get_open(self, invitation_id: uuid.UUID) -> Invitation | None:
        """The unaccepted invitation ``invitation_id`` of this org, or ``None``."""
        return await self.session.scalar(
            select(Invitation).where(*self._open(), Invitation.id == invitation_id)
        )

    async def revoke(self, invitation_id: uuid.UUID) -> bool:
        """Delete the unaccepted invitation ``invitation_id``; whether one was deleted."""
        statement = (
            delete(Invitation).where(*self._open(), Invitation.id == invitation_id).returning(Invitation.id)
        )
        return await self.session.scalar(statement) is not None

    async def consume(self, invitation_id: uuid.UUID, now: datetime) -> Invitation | None:
        """Mark the invitation accepted at ``now`` if it is still open and unexpired; ``None`` otherwise.

        One conditional ``UPDATE``, so concurrent acceptances cannot both succeed.
        """
        statement = (
            update(Invitation)
            .where(*self._open(), Invitation.id == invitation_id, Invitation.expires_at > now)
            .values(accepted_at=now)
            .returning(Invitation)
        )
        return await self.session.scalar(statement)
